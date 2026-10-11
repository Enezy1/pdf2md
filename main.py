import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from app.selftest import run as selftest
        sys.exit(selftest())
    from app.ui.main_window import run
    run()
