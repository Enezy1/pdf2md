# بناء نسخة exe (مجلد واحد: dist\pdf2md\pdf2md.exe):
#     python -m PyInstaller pdf2md.spec --noconfirm
# الإعدادات والمفاتيح والنماذج تبقى في %APPDATA%\pdf2md كما في python main.py.
# PaddleOCR لا يُضمَّن هنا: يُنزَّل من الإعدادات مع بايثون مضمَّن خاص به (engines\paddle\python).

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# كل وحدات البرنامج (بعضها يُستورد داخل الدوال) — عدا paddle_worker: ملف بيانات يشغّله بايثون المحرّك،
# ولو حُلّل هنا لسحب onnxruntime وopencv إلى البرنامج الرئيسي.
hidden = [m for m in collect_submodules("app") if not m.endswith("paddle_worker")]

datas = [
    (str(ROOT / "resources"), "resources"),
    (str(ROOT / "app" / "paddle_worker.py"), "app"),
]
datas += collect_data_files("customtkinter")
datas += collect_data_files("sv_ttk")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    # مكتبات ثقيلة موجودة في بايثون الجهاز ولا يستخدمها البرنامج (تُسحب عبر استيرادات اختيارية لمكتبات أخرى)
    excludes=["onnxruntime", "cv2", "pyclipper", "pytest", "matplotlib", "IPython", "tests",
              "torch", "functorch", "torchvision", "torchaudio", "scipy", "pandas", "pyarrow", "sympy", "networkx",
              "transformers", "sklearn", "tensorflow", "grpc", "hf_xet", "huggingface_hub", "lxml", "openai",
              "fsspec", "jinja2", "notebook", "jupyter_client", "zmq", "tornado", "numba", "llvmlite"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pdf2md",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="pdf2md", upx=False)
