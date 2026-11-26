import os
import sys


def _startup_failure() -> None:
    """خطأ أثناء بدء التشغيل: نكتبه في السجل ونعرض رسالة.

    النسخة المبنية (exe) بلا نافذة طرفية، فأي استثناء كان يختفي بصمت: لا نافذة،
    ولا رسالة، ولا أثر — وكان يبدو للمستخدم كأن "مدخل الكليك اليمين لا يعمل"
    على أجهزة أخرى. الآن يظهر السبب، ويُحفظ في logs\\crash.log.
    """
    import traceback

    detail = traceback.format_exc()
    try:
        from app.config import app_data_dir

        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(detail + "\n")
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "pdf2md: تعذّر بدء البرنامج.\n\n" + detail[:1500],
            "pdf2md",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from app.selftest import run as selftest
        sys.exit(selftest())
    try:
        from app.ui.main_window import run
        # المسار قد يأتي من سطر الأوامر (فتح عبر كليك يمين) أو من متغير البيئة
        # PDF2MD_TARGET — الثاني احتياط لأن ويندوز لا يضمن بقاء الوسائط بعد
        # إعادة تشغيل العملية من طرف التطبيق.
        targets = [a for a in sys.argv[1:] if a]
        env_target = os.environ.get("PDF2MD_TARGET", "").strip()
        if env_target and env_target not in targets:
            targets.append(env_target)
        run(targets)
    except Exception:
        _startup_failure()
        sys.exit(1)
