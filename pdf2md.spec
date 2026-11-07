# بناء نسخة exe:
#     python -m PyInstaller pdf2md.spec --noconfirm
#   dist\pdf2md.exe          ملف واحد يحمل كل شيء — هذا الذي يُنقل لأي جهاز (لا يحتاج بايثون ولا مجلداً بجانبه)
#   dist\pdf2md\pdf2md.exe   نسخة المجلد (أسرع فتحاً، لكن يجب نقل المجلد كاملاً مع _internal)
# الإعدادات والمفاتيح والنماذج تبقى في %APPDATA%\pdf2md كما في python main.py.
# PaddleOCR لا يُضمَّن هنا: يُنزَّل من الإعدادات مع بايثون مضمَّن خاص به (engines\paddle\python).

from pathlib import Path
import re

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# اسم الشركة والنشر: يظهر في خصائص الملف، وفي لوحة التحكم ← البرامج بعد التثبيت.
PUBLISHER = "Enezy1"
DESCRIPTION = "تحويل ملفات PDF إلى Markdown"


def app_version() -> str:
    """رقم الإصدار من app/__init__.py — مصدر واحد للبرنامج والمثبّت وملف الإصدار."""
    txt = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else "0.0.0"


def version_resource() -> str:
    """ملف موارد النسخة (VSVersionInfo): اسم المنتج والشركة والوصف.

    بدونه يبقى الملف بلا وصف، فيظهر في خصائصه وفي لوحة التحكم باسم غير مفهوم.
    """
    v = app_version()
    nums = [int(x) for x in re.findall(r"\d+", v)[:4]]
    nums += [0] * (4 - len(nums))
    quad = ", ".join(str(x) for x in nums)
    txt = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}),
    prodvers=({quad}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', '{PUBLISHER}'),
         StringStruct('FileDescription', '{DESCRIPTION}'),
         StringStruct('FileVersion', '{v}'),
         StringStruct('InternalName', 'pdf2md'),
         StringStruct('OriginalFilename', 'pdf2md.exe'),
         StringStruct('ProductName', 'pdf2md'),
         StringStruct('ProductVersion', '{v}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)"""
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    path = build / "version_info.txt"
    path.write_text(txt, encoding="utf-16")  # PyInstaller يقرأ ملف النسخة بترميز UTF-16
    return str(path)

# كل وحدات البرنامج (بعضها يُستورد داخل الدوال) — عدا paddle_worker: ملف بيانات يشغّله بايثون المحرّك،
# ولو حُلّل هنا لسحب onnxruntime وopencv إلى البرنامج الرئيسي.
hidden = [m for m in collect_submodules("app") if not m.endswith("paddle_worker")]

datas = [
    (str(ROOT / "resources"), "resources"),
    (str(ROOT / "assets"), "assets"),  # الشعار وأيقونة النافذة
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
    icon=str(ROOT / "assets" / "pdf2md.ico"),
    version=version_resource(),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="pdf2md", upx=False)

# نسخة الملف الواحد: كل المكتبات داخل exe وتُفك في مجلد مؤقت عند الفتح (أبطأ بثوانٍ، لكن لا شيء ينقص عند النقل).
onefile = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pdf2md",
    console=False,
    icon=str(ROOT / "assets" / "pdf2md.ico"),
    version=version_resource(),
    upx=False,
    runtime_tmpdir=None,
)
