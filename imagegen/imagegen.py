#!/usr/bin/env python3
# ---
# name: imagegen
# description: Generate images via OpenRouter (default) or Poe, reusing fir's stored credentials. Auto-selects the current best image model from a live catalog.
# ---
"""imagegen.py — image generation as a tool, for text-only agent models.

Why
---
fir's driving model is a text/agent model; its catalog only tracks *input*
modalities. Image generation therefore cannot be "the model" — it has to be
a tool. This extension calls an OpenAI-compatible chat-completions endpoint
with ``modalities: ["image","text"]``, writes the resulting PNG to disk and
hands back a path the agent can deliver (e.g. poe-acp's ``poe-attach``).

Provider policy
---------------
OpenRouter is the default *for budget isolation*, not price (Poe and
OpenRouter charge the same for the nano-banana family). On a Poe-hosted
relay, image spend and the bot's own survival share one points pool:
draining it kills the control channel until the month rolls over. Draining
OpenRouter only costs you images. So Poe is never selected implicitly — it
must be named, per call or in config.

Model selection
---------------
Models are NOT pinned by default. Each provider's ``/models`` endpoint is
queried live (24h disk cache) and filtered to what can actually emit images:

* OpenRouter — ``architecture.output_modalities`` contains ``image``
* Poe — ``supported_endpoints`` contains ``/v1/chat/completions`` AND
  ``pricing.image`` is set. (Most Poe image bots expose no OpenAI-compatible
  endpoint at all, so the usable subset is small.)

Ranking uses per-image price as a capability proxy: flagships cost more.
``quality=best`` takes the priciest, ``quality=fast`` the cheapest, newest
wins ties. A new flagship is therefore picked up automatically the day it
lands, with no code change and no fir release.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import fir_ext

CONFIG_PATH = Path.home() / ".config" / "fir" / "imagegen.json"
AUTH_PATH = Path(os.environ.get("FIR_AUTH_FILE", Path.home() / ".config" / "fir" / "auth.json"))
CACHE_DIR = Path.home() / ".cache" / "fir"
OUT_DIR = Path.home() / "imagegen-out"
CATALOG_TTL = 24 * 3600
POE_BALANCE_URL = "https://api.poe.com/usage/current_balance"
DEFAULT_WARN_AFTER = 10
DEFAULT_POE_FLOOR = 200_000  # points; warn below this

_SESSION_COUNT_KEY = "imagegen_count"

PROVIDERS = {
    "openrouter": {
        "base": "https://openrouter.ai/api/v1",
        "auth_slot": "openrouter",
        "env": "OPENROUTER_API_KEY",
    },
    "poe": {
        "base": "https://api.poe.com/v1",
        "auth_slot": "poe",
        "env": "POE_API_KEY",
    },
}


# --------------------------------------------------------------------------
# config / credentials
# --------------------------------------------------------------------------


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def _api_key(provider: str) -> str | None:
    """Resolve a provider key. Never logged, never returned to the model."""
    spec = PROVIDERS[provider]
    env = os.environ.get(spec["env"])
    if env:
        return env.strip()
    try:
        auth = json.loads(AUTH_PATH.read_text())
    except Exception:
        return None
    slot = auth.get(spec["auth_slot"]) or {}
    key = slot.get("access") or slot.get("key") or slot.get("api_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _available() -> list[str]:
    return [p for p in ("openrouter", "poe") if _api_key(p)]


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


def _http_json(url: str, key: str, payload: dict | None = None, timeout: int = 180) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:400]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from None


# --------------------------------------------------------------------------
# catalog — live, cached, ranked
# --------------------------------------------------------------------------


def _price_per_image(model: dict) -> float:
    pricing = model.get("pricing") or {}
    for field in ("image_output", "image", "completion"):
        raw = pricing.get(field)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 0.0


def _filter_openrouter(models: list[dict]) -> list[dict]:
    out = []
    for m in models:
        arch = m.get("architecture") or {}
        if "image" not in (arch.get("output_modalities") or []):
            continue
        mid = m.get("id", "")
        # routing pseudo-models are not real image models
        if mid.startswith("openrouter/auto"):
            continue
        out.append(m)
    return out


def _filter_poe(models: list[dict]) -> list[dict]:
    out = []
    for m in models:
        eps = m.get("supported_endpoints") or []
        if "/v1/chat/completions" not in eps:
            continue
        if not (m.get("pricing") or {}).get("image"):
            continue
        out.append(m)
    return out


def _fetch_catalog(provider: str, force: bool = False) -> list[dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"imagegen-catalog-{provider}.json"
    if not force and cache.exists() and (time.time() - cache.stat().st_mtime) < CATALOG_TTL:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    key = _api_key(provider)
    if not key:
        raise RuntimeError(f"no credential for {provider}")
    raw = _http_json(f"{PROVIDERS[provider]['base']}/models", key, timeout=60)
    models = raw.get("data") or []
    picked = _filter_openrouter(models) if provider == "openrouter" else _filter_poe(models)
    slim = [
        {
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "price": _price_per_image(m),
            "created": m.get("created") or 0,
        }
        for m in picked
        if m.get("id")
    ]
    try:
        cache.write_text(json.dumps(slim))
    except Exception:
        pass
    return slim


def _rank(catalog: list[dict], quality: str) -> list[dict]:
    """Price is a capability proxy: flagships cost more. Newest breaks ties."""
    reverse = quality != "fast"
    return sorted(catalog, key=lambda m: (m["price"], m["created"]), reverse=reverse)


def _auto_model(provider: str, quality: str) -> dict:
    catalog = _fetch_catalog(provider)
    if not catalog:
        raise RuntimeError(f"{provider}: no image-capable models in catalog")
    return _rank(catalog, quality)[0]


def _resolve_model(provider: str, model: str | None, quality: str) -> tuple[str, float, str]:
    """Return (model_id, price_per_image, how_chosen)."""
    catalog = _fetch_catalog(provider)
    if model:
        for m in catalog:
            if m["id"] == model or m["id"].endswith("/" + model):
                return m["id"], m["price"], "explicit"
        # Pinned model gone: fail loudly with the live list rather than
        # silently substituting — a different image model is a different
        # product, not a drop-in.
        ids = ", ".join(m["id"] for m in catalog[:12]) or "(none)"
        raise RuntimeError(f"{provider}: model {model!r} not in live catalog. Available: {ids}")
    chosen = _auto_model(provider, quality)
    return chosen["id"], chosen["price"], f"auto/{quality}"


# --------------------------------------------------------------------------
# poe balance preflight
# --------------------------------------------------------------------------


def _poe_balance() -> int | None:
    key = _api_key("poe")
    if not key:
        return None
    try:
        data = _http_json(POE_BALANCE_URL, key, timeout=20)
        return int(data.get("current_point_balance"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# image extraction
# --------------------------------------------------------------------------

# Poe returns the image as a CDN link inside markdown; stop at markdown /
# quote delimiters so the trailing ")" never ends up in the fetched URL.
_MD_URL = re.compile(r"https://[^\s)\]\"'<>]+")


def _download(url: str) -> bytes:
    # poecdn 403s a bare urllib UA.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fir imagegen)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _extract_images(message: dict) -> list[bytes]:
    blobs: list[bytes] = []
    for img in message.get("images") or []:
        url = ((img.get("image_url") or {}).get("url")) if isinstance(img, dict) else None
        if not url:
            continue
        if url.startswith("data:"):
            blobs.append(base64.b64decode(url.split(",", 1)[1]))
        else:
            blobs.append(_download(url))
    if blobs:
        return blobs
    # Poe returns a markdown/plain CDN link in the text content instead.
    content = message.get("content")
    text = content if isinstance(content, str) else json.dumps(content)
    seen = set()
    errors = []
    for url in _MD_URL.findall(text or ""):
        if url in seen:
            continue
        seen.add(url)
        try:
            blobs.append(_download(url))
        except Exception as exc:
            errors.append(f"{url[:60]}...: {exc}")
    if not blobs and errors:
        raise RuntimeError("; ".join(errors[:2]))
    return blobs


# --------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _heartbeat(ctx: "fir_ext.Context", label: str, every: float = 5.0):
    """Keep the host-side tool_call deadline alive during a silent HTTP wait.

    fir's tool_call timeout is *activity-aware*: any message the extension
    sends resets it. The host's own keepAlive only covers calls it drives
    (side_query / call_tool) — an extension blocking in urllib looks dead.
    A periodic report_progress is therefore both the UI spinner text and the
    liveness signal, so a slow image model can never be clipped mid-render
    regardless of the declared timeout.
    """
    stop = threading.Event()

    def beat():
        n = 0
        while not stop.wait(every):
            n += 1
            with contextlib.suppress(Exception):
                ctx.report_progress(f"{label} {n * int(every)}s")

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


@fir_ext.tool(
    name="generate_image",
    description=(
        "Generate an image from a text prompt and save it as a PNG on this host. "
        "Returns the file path — deliver it to the user with the relay's attach "
        "directive (e.g. poe-attach ... inline). Defaults to OpenRouter; pass "
        "provider='poe' to bill Poe points instead (never chosen implicitly, "
        "because Poe spend can take the relay itself offline). Model is "
        "auto-selected from a live catalog unless you name one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to draw."},
            "provider": {
                "type": "string",
                "enum": ["openrouter", "poe"],
                "description": "Override the configured provider for this call.",
            },
            "model": {
                "type": "string",
                "description": "Explicit model id. Omit to auto-select the current best.",
            },
            "quality": {
                "type": "string",
                "enum": ["best", "fast"],
                "description": "Auto-selection tier: 'best' (flagship) or 'fast' (cheapest). Default best.",
            },
            "image": {
                "type": "string",
                "description": "Optional path to an input image to edit rather than generate from scratch.",
            },
            "all_variants": {
                "type": "boolean",
                "description": "Keep every frame the model emits, including interim renders. Default false (final only).",
            },
        },
        "required": ["prompt"],
    },
    display_hint={"title_args": [{"name": "prompt", "style": "accent"}], "result_max_lines": 6},
    # Image models routinely take 30-120s; the 30s default would clip them.
    # The heartbeat below resets this on every beat, so it is a floor on how
    # long a *silent* call may run, not a ceiling on the render.
    timeout=300,
)
def generate_image(params: dict, ctx: fir_ext.Context) -> dict:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return _err("prompt is required")

    cfg = _load_config()
    quality = params.get("quality") or cfg.get("quality") or "best"
    explicit_provider = params.get("provider")
    provider = explicit_provider or cfg.get("provider") or "openrouter"

    if provider not in PROVIDERS:
        return _err(f"unknown provider {provider!r}")

    have = _available()
    if not have:
        return _err(
            "no image provider credential. Add OPENROUTER_API_KEY (or an "
            "'openrouter' slot in auth.json), or say 'use poe' if a Poe key exists."
        )

    # Poe is opt-in. It is never reached by falling through.
    if provider == "poe" and not explicit_provider and cfg.get("provider") != "poe":
        provider = "openrouter"
    if provider not in have:
        alt = have[0]
        if provider == "poe":
            return _err("provider 'poe' requested but no Poe credential found")
        return _err(
            f"no credential for {provider}. Available: {', '.join(have)}. "
            f"Say 'use {alt}' or set it with /imagegen provider {alt}."
        )

    notes: list[str] = []

    if provider == "poe":
        floor = int(cfg.get("poe_floor") or DEFAULT_POE_FLOOR)
        bal = _poe_balance()
        if bal is not None:
            notes.append(f"poe balance {bal:,} pts")
            if bal < floor:
                return _err(
                    f"Poe balance {bal:,} pts is below the floor of {floor:,}. "
                    "Refusing: draining Poe points takes the relay offline. "
                    "Use OpenRouter, or raise poe_floor in imagegen.json."
                )

    try:
        model_id, price, how = _resolve_model(provider, params.get("model") or cfg.get("model"), quality)
    except Exception as exc:
        return _err(str(exc))

    content: Any = prompt
    img_path = params.get("image")
    if img_path:
        p = Path(img_path).expanduser()
        if not p.exists():
            return _err(f"input image not found: {p}")
        b64 = base64.b64encode(p.read_bytes()).decode()
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]

    payload = {
        "model": model_id,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": content}],
    }

    key = _api_key(provider)
    try:
        with _heartbeat(ctx, model_id.split("/")[-1]):
            resp = _http_json(f"{PROVIDERS[provider]['base']}/chat/completions", key, payload)
    except Exception as exc:
        return _err(f"{provider}/{model_id}: {exc}")

    choices = resp.get("choices") or []
    if not choices:
        return _err(f"{provider}/{model_id}: empty response")
    message = choices[0].get("message") or {}

    try:
        with _heartbeat(ctx, "downloading"):
            blobs = _extract_images(message)
    except Exception as exc:
        return _err(f"image fetch failed: {exc}")
    if not blobs:
        txt = message.get("content")
        snippet = (txt if isinstance(txt, str) else json.dumps(txt))[:200]
        return _err(f"{model_id} returned no image. Text was: {snippet}")

    # Gemini image models emit progressive frames: an interim render followed
    # by the refined final. Keep only the last unless variants are asked for.
    dropped = 0
    if len(blobs) > 1 and not params.get("all_variants"):
        dropped = len(blobs) - 1
        blobs = blobs[-1:]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = []
    for idx, blob in enumerate(blobs):
        suffix = "" if len(blobs) == 1 else f"-{idx + 1}"
        path = OUT_DIR / f"img-{stamp}{suffix}.png"
        path.write_bytes(blob)
        paths.append(str(path))

    # Runaway-loop warning — a nudge, never a hard stop.
    count = _bump_count(ctx, len(paths))
    warn_after = int(cfg.get("warn_after") or DEFAULT_WARN_AFTER)
    if count >= warn_after:
        notes.append(f"⚠ {count} images this session (~${count * price:.2f}) — still going?")

    if dropped:
        notes.append(f"({dropped} interim frame(s) discarded)")
    cost = f"~${price:.5f}/img" if price else "price unknown"
    lines = [f"saved: {p}" for p in paths]
    lines.append(f"{provider}/{model_id} [{how}] {cost}")
    lines.extend(notes)
    lines.append("Deliver it with the relay attach directive (inline) — do not paste the bytes.")
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "is_error": False,
        "details": {"paths": paths, "provider": provider, "model": model_id, "price": price},
    }


@fir_ext.tool(
    name="image_models",
    description=(
        "List the image-capable models currently available, ranked best-first, "
        "with per-image price. Live catalog (24h cache) — reflects new releases "
        "without a code change."
    ),
    parameters={
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["openrouter", "poe"]},
            "refresh": {"type": "boolean", "description": "Bypass the 24h cache."},
        },
    },
    timeout=90,
)
def image_models(params: dict, ctx: fir_ext.Context) -> dict:
    providers = [params["provider"]] if params.get("provider") else _available()
    if not providers:
        return _err("no provider credentials configured")
    out = []
    for prov in providers:
        try:
            catalog = _rank(_fetch_catalog(prov, force=bool(params.get("refresh"))), "best")
        except Exception as exc:
            out.append(f"{prov}: {exc}")
            continue
        out.append(f"{prov} ({len(catalog)} image models):")
        for m in catalog[:12]:
            out.append(f"  {m['id']:<45} ${m['price']:.5f}/img")
    return {"content": [{"type": "text", "text": "\n".join(out)}], "is_error": False}


@fir_ext.command(
    name="imagegen",
    description="Show or set the image provider/model. `/imagegen provider openrouter|poe`, `/imagegen model <id>`, `/imagegen model auto`.",
)
def cmd_imagegen(args: list, ctx: fir_ext.Context) -> dict:
    cfg = _load_config()
    if not args:
        have = ", ".join(_available()) or "none"
        msg = [
            f"provider: {cfg.get('provider') or 'openrouter (default)'}",
            f"model:    {cfg.get('model') or 'auto'}",
            f"quality:  {cfg.get('quality') or 'best'}",
            f"creds:    {have}",
        ]
        bal = _poe_balance()
        if bal is not None:
            msg.append(f"poe:      {bal:,} pts")
        return {"message": "\n".join(msg), "print_response": False}

    key = args[0].lower()
    val = args[1] if len(args) > 1 else None
    if key == "provider" and val in PROVIDERS:
        cfg["provider"] = val
        cfg.pop("model", None)  # a model from the old provider is meaningless
        _save_config(cfg)
        return {"message": f"imagegen provider -> {val} (model reset to auto)", "print_response": False}
    if key == "model" and val:
        if val == "auto":
            cfg.pop("model", None)
        else:
            cfg["model"] = val
        _save_config(cfg)
        return {"message": f"imagegen model -> {val}", "print_response": False}
    if key == "quality" and val in ("best", "fast"):
        cfg["quality"] = val
        _save_config(cfg)
        return {"message": f"imagegen quality -> {val}", "print_response": False}
    return {"message": "usage: /imagegen [provider openrouter|poe | model <id>|auto | quality best|fast]", "print_response": False}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _bump_count(ctx: fir_ext.Context, n: int) -> int:
    try:
        cur = int(ctx.get_state(_SESSION_COUNT_KEY) or 0)
    except Exception:
        cur = 0
    cur += n
    try:
        ctx.set_state(_SESSION_COUNT_KEY, str(cur))
    except Exception:
        pass
    return cur


fir_ext.run(name="imagegen")
