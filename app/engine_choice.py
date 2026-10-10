"""أي محرّك داخلي يقرأ الصفحات المصوّرة فعلاً، حسب المثبّت والمفعّل.

القاعدة: ما اختاره المستخدم ("تفعيل" في تبويب داخلي) إن كان مثبّتاً، وإلا المثبّت الوحيد المتاح.
فإذا نُزّل PaddleOCR ولا يوجد نموذج محلي مثبّت، يصير PaddleOCR هو المحرّك دون أي إعداد.
"""

from __future__ import annotations

from . import model_manager as mm
from . import models_catalog as cat

LOCAL, PADDLE, PADDLE_LOCAL, NONE = "local", "paddle", "paddle_local", "none"


def selected_spec(settings) -> cat.ModelSpec | None:
    ids = [m.id for m in cat.MODELS]
    return cat.MODELS[ids.index(settings.local_model)] if settings.local_model in ids else None


def llama_ready(spec: cat.ModelSpec | None) -> bool:
    return bool(spec and mm.is_installed(spec) and mm.llama_exe())


def ready_llama_spec(settings) -> cat.ModelSpec | None:
    """النموذج المحلي المختار إن كان جاهزاً، وإلا أول نموذج مثبّت (مع llama-server)."""
    spec = selected_spec(settings)
    if llama_ready(spec):
        return spec
    if mm.llama_exe():
        return next((m for m in cat.MODELS if mm.is_installed(m)), None)
    return None


def effective(settings, paddle_ok: bool | None = None) -> tuple[str, cat.ModelSpec | None]:
    """(الوضع، نموذج llama إن لزم). الوضع: local | paddle | paddle_local | none."""
    if paddle_ok is None:
        from . import paddle_engine as pe
        paddle_ok = pe.is_available()
    spec = ready_llama_spec(settings)
    want = settings.scan_engine
    if paddle_ok and want == PADDLE:
        return PADDLE, None
    if paddle_ok and want == PADDLE_LOCAL:
        return (PADDLE_LOCAL, spec) if spec else (PADDLE, None)
    if spec:
        return LOCAL, spec
    if paddle_ok:
        return PADDLE, None
    return NONE, None


def label(mode: str, spec: cat.ModelSpec | None) -> str:
    return {LOCAL: spec.label if spec else "", PADDLE: "PaddleOCR",
            PADDLE_LOCAL: f"PaddleOCR ثم {spec.label if spec else ''}", NONE: ""}[mode]
