import os
import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from app.selftest import run as selftest
        sys.exit(selftest())
    from app.ui.main_window import run
    # المسار قد يأتي من سطر الأوامر (فتح عبر كليك يمين) أو من متغير البيئة
    # PDF2MD_TARGET — الثاني احتياط لأن ويندوز لا يضمن بقاء الوسائط بعد
    # إعادة تشغيل العملية من طرف التطبيق.
    targets = [a for a in sys.argv[1:] if a]
    env_target = os.environ.get("PDF2MD_TARGET", "").strip()
    if env_target and env_target not in targets:
        targets.append(env_target)
    run(targets)
