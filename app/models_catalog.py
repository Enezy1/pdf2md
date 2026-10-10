"""قائمة النماذج البصرية المحلية (GGUF + mmproj) ونسخة llama-server.

كل اسم مستودع وملف وحجم وبصمة SHA-256 هنا تم التحقق منه من واجهة Hugging Face
(/api/models/<repo>/tree/main) ومن GitHub API لإصدارات llama.cpp بتاريخ 2026-10-05.
البصمة إلزامية: أي ملف لا يطابقها يُرفض (سواء نُزّل أو نُسخ من فلاشة).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FileSpec:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    repo: str
    model: FileSpec
    mmproj: FileSpec
    note: str = ""

    def url(self, f: FileSpec) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{f.name}"

    @property
    def total_size(self) -> int:
        return self.model.size + self.mmproj.size


MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "qwen3vl-4b", "Qwen3-VL 4B (Q4_K_M)", "Qwen/Qwen3-VL-4B-Instruct-GGUF",
        FileSpec("Qwen3VL-4B-Instruct-Q4_K_M.gguf", 2497281664, "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a"),
        FileSpec("mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", 453974304, "30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d"),
    ),
    ModelSpec(
        "qwen3vl-8b", "Qwen3-VL 8B (Q4_K_M)", "Qwen/Qwen3-VL-8B-Instruct-GGUF",
        FileSpec("Qwen3VL-8B-Instruct-Q4_K_M.gguf", 5027784800, "67d1659bfe71b89d50b45a4ad1a9e5b997e5bb16ce5da66a6a6167abd569e9e2"),
        FileSpec("mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf", 752289728, "c6ba85508d82f42590e6eb77d5340369ab6fecf107a7561d809523d8aa5f3bfd"),
        "بطيء على CPU، يحتاج ذاكرة ≥ 12GB",
    ),
    ModelSpec(
        "qwen25vl-3b", "Qwen2.5-VL 3B (Q4_K_M)", "ggml-org/Qwen2.5-VL-3B-Instruct-GGUF",
        FileSpec("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf", 1929901056, "d02fe9b69ad8cadbbd228e387667af66612c44bed29ffc8eb1e7caf9ac486c12"),
        FileSpec("mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf", 844757728, "980c9b2f78c04e6cff93d277ada09e768394f112d75db3b4e9dea8a69f9fb904"),
    ),
    ModelSpec(
        "qwen25vl-7b", "Qwen2.5-VL 7B (Q4_K_M)", "ggml-org/Qwen2.5-VL-7B-Instruct-GGUF",
        FileSpec("Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf", 4683072032, "9258bf05b12686d097ff3b6b18d968ab393649780aa2b3cd67fec43d50554392"),
        FileSpec("mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf", 853119712, "2ddb555391bae966e412deab9e07b58afa18bcc06930ba0f1c78a3695ab9e506"),
    ),
    ModelSpec(
        "gemma3-4b", "Gemma 3 4B (Q4_K_M)", "ggml-org/gemma-3-4b-it-GGUF",
        FileSpec("gemma-3-4b-it-Q4_K_M.gguf", 2489757856, "882e8d2db44dc554fb0ea5077cb7e4bc49e7342a1f0da57901c0802ea21a0863"),
        FileSpec("mmproj-model-f16.gguf", 851251104, "8c0fb064b019a6972856aaae2c7e4792858af3ca4561be2dbf649123ba6c40cb"),
    ),
    ModelSpec(
        "nanonets-ocr2-3b", "Nanonets-OCR2 3B (Q4_K_M)", "mradermacher/Nanonets-OCR2-3B-GGUF",
        FileSpec("Nanonets-OCR2-3B.Q4_K_M.gguf", 1929901888, "d4f5bae51febb85cf39e90da47aae519a48a7c8ed233a8d0a81565ccaaad52b0"),
        FileSpec("Nanonets-OCR2-3B.mmproj-Q8_0.gguf", 847768704, "256cd2427d6c5754d8572d00e986ca4814f35f0681e9bea97c26bc3c0cf772b1"),
        "مستودع تحويل من طرف ثالث (mradermacher)، ليس من nanonets",
    ),
    ModelSpec(
        "nanonets-ocr-s", "Nanonets-OCR-s (Q4_K_M)", "unsloth/Nanonets-OCR-s-GGUF",
        FileSpec("Nanonets-OCR-s-Q4_K_M.gguf", 1929903296, "d90c1c2edf08bff41904c658e443edaebce36c728a4125ad5ed37fa7fd608aa2"),
        FileSpec("mmproj-F16.gguf", 1338428576, "7755decbf0d29fa73c48945d942e1bc7b148c0d9dd68c22799739024d4956d12"),
        "مستودع تحويل من طرف ثالث (unsloth)",
    ),
    ModelSpec(
        "olmocr2-7b", "olmOCR-2 7B (Q4_K_M)", "lmstudio-community/olmOCR-2-7B-1025-GGUF",
        FileSpec("olmOCR-2-7B-1025-Q4_K_M.gguf", 4683072032, "821ed0a31450d7d265bcc05519a28ebb43eb3cd2738ca543fa11b78f35153e25"),
        FileSpec("mmproj-olmOCR-2-7B-1025-F16.gguf", 1354162944, "fe90b9db826810aa37a17b53bd575ee15a9947e9075d73a8f68e284a38ab5eb4"),
        "مستودع تحويل من طرف ثالث (lmstudio-community)؛ مدرّب أساساً على الإنجليزية",
    ),
    ModelSpec(
        "dots-ocr", "dots.ocr (Q8_0)", "ggml-org/dots.ocr-GGUF",
        FileSpec("dots.ocr-Q8_0.gguf", 1894530272, "6f2db1a75215893760b7c7185d52f11715d2e0cb7bff9cefd3f6d409fa1de7e9"),
        FileSpec("mmproj-dots.ocr-Q8_0.gguf", 1344068512, "20d3d35bea3a64ead0b4b213f8db0de136e174bb5b75b8e85786c51e8e822423"),
        "لا يوجد Q4 في المستودع الرسمي",
    ),
    ModelSpec(
        "minicpm-v45", "MiniCPM-V 4.5 (Q4_K_M)", "openbmb/MiniCPM-V-4_5-gguf",
        FileSpec("MiniCPM-V-4_5-Q4_K_M.gguf", 5026714304, "c1c3c33100b15b4caf7319acce4e23c0eb0ce1cbd12f70e8d24f05aa67b7512f"),
        FileSpec("mmproj-model-f16.gguf", 1095113184, "7a7225a32e8d453aaa3d22d8c579b5bf833c253f784cdb05c99c9a76fd616df8"),
    ),
)

# الترتيب في الإعدادات من الأفضل للأسوأ، حسب التجربة على مذكرة حقيقية (ناسكو، فاكس 100 نقطة، 2026-10-11 —
# التفاصيل في DEVLOG). "paddle" = PaddleOCR. ما لم يُجرَّب يأتي بعد المجرَّب بترتيب الكتالوج.
RANKING = ("dots-ocr", "qwen3vl-4b", "nanonets-ocr2-3b", "paddle")
VERDICTS = {
    "dots-ocr": "١ · الأدق، بطيء جداً",
    "qwen3vl-4b": "٢ · أرقام جيدة، كلمات خاطئة أحياناً",
    "nanonets-ocr2-3b": "٣ · أخطأ في مبلغ وأسقط عبارة",
    "paddle": "٤ · سريع جداً، مسودة",
}
UNTESTED = "لم يُجرَّب بعد"
# الأرقام في VERDICTS = الترتيب في التجربة. على معالج i7 بلا كرت: dots ~11 د/صفحة، Qwen3-VL 4B ~5.5، Nanonets ~4.5،
# Paddle ~10 ث. dots: كل أرقام الصفحة صحيحة؛ Qwen: "المدعية" ← "المدنية"؛ Nanonets: 250,000 ← 250.


def rank(model_id: str) -> int:
    return RANKING.index(model_id) if model_id in RANKING else len(RANKING)


MODELS = tuple(sorted(MODELS, key=lambda m: rank(m.id)))  # sorted ثابت: غير المجرَّب يبقى بترتيبه

# نماذج بحثنا عنها ولم نجد لها GGUF + mmproj صالحاً للتحقق — تظهر في benchmark كمستبعدة.
EXCLUDED: dict[str, str] = {
    "Qwen3-VL 2B (Q4_K_M)": "أُلغي: فشل في قراءة المستندات العربية الممسوحة (حلقات تكرار ونص مختلَق في اختبار 2026-10-05)",
    "olmOCR-2 (allenai الرسمي)": "لا يوجد مستودع GGUF رسمي من allenai (allenai/olmOCR-2-7B-1025-GGUF غير موجود)؛ استُخدمت نسخة lmstudio-community بدلاً منه",
    "Nanonets-OCR-s (nanonets الرسمي)": "nanonets/Nanonets-OCR-s-GGUF غير موجود؛ استُخدمت نسخة unsloth",
    "MiniCPM-V 4.5 (ggml-org)": "ggml-org/MiniCPM-V-4_5-GGUF غير موجود؛ استُخدم المستودع الرسمي openbmb",
}


def get(model_id: str) -> ModelSpec:
    for m in MODELS:
        if m.id == model_id:
            return m
    raise KeyError(model_id)


# llama-server لويندوز (نسخة CPU، x64) — إصدار مثبّت، البصمة من GitHub API.
LLAMA_TAG = "b11398"
LLAMA_ZIP = FileSpec(
    "llama-b11398-bin-win-cpu-x64.zip", 19386495,
    "a5b932cb28b2f17da93bf7111c7cbd1324cffa3a6f474aa10b616908868037a2",
)
LLAMA_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/{LLAMA_ZIP.name}"
