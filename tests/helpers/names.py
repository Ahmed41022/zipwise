"""Realistic multi-word file names per legacy code page, used by the encoding tests."""

from __future__ import annotations

from pathlib import Path

from helpers.builders import file_entry, write_raw_zip

ARABIC = [
    "تقرير المبيعات.docx",
    "صور العائلة/الرحلة.jpg",
    "فاتورة شهر مارس.pdf",
    "ملاحظات الاجتماع.txt",
    "كتاب الرياضيات/الفصل الأول.pdf",
    "سيرة ذاتية.docx",
]

RUSSIAN = [
    "Отчёт по продажам.docx",
    "Семейные фотографии/Поездка.jpg",
    "Договор аренды квартиры.pdf",
    "Протокол собрания.txt",
    "Список покупок.txt",
    "Курсовая работа по физике.docx",
]

GREEK = [
    "Αναφορά πωλήσεων.docx",
    "Οικογενειακές φωτογραφίες/Ταξίδι.jpg",
    "Συμβόλαιο ενοικίασης.pdf",
    "Σημειώσεις συνάντησης.txt",
    "Λογαριασμός ρεύματος Μαρτίου.pdf",
]

HEBREW = [
    "דוח מכירות.docx",
    "תמונות משפחה/טיול.jpg",
    "חשבונית חודש מרץ.pdf",
    "סיכום פגישה.txt",
    "קורות חיים.docx",
]

JAPANESE = [
    "売上報告書.xlsx",
    "家族の写真/旅行.jpg",
    "議事録 2024年3月.docx",
    "見積書（最終版）.pdf",
    "プロジェクト計画書.pptx",
    "夏休みの宿題.txt",
]

SIMPLIFIED = [
    "销售报告.docx",
    "家庭照片/旅行.jpg",
    "会议记录 2024年3月.docx",
    "项目计划书.xlsx",
    "合同草稿（最终版）.pdf",
    "学习笔记.txt",
]

TRADITIONAL = [
    "銷售報告.docx",
    "家庭照片/旅行.jpg",
    "會議記錄 2024年3月.docx",
    "專案計畫書.xlsx",
    "合約草稿（最終版）.pdf",
    "學習筆記.txt",
]

KOREAN = [
    "매출 보고서.xlsx",
    "가족 사진/여행.jpg",
    "회의록 2024년 3월.docx",
    "프로젝트 계획서.pptx",
    "계약서 최종본.pdf",
    "여름방학 숙제.txt",
]

# Characters that exist in both CP437 and CP850 would make the two indistinguishable, so the
# CP850 names use letters only CP850 has (ø, Ø, ã, õ, Á).
WEST_CP437 = [
    "Café menu.txt",
    "Müller Übersicht 2024.xlsx",
    "señor niño.docx",
    "Año nuevo/fotos.jpg",
    "Façade à Paris.png",
]

WEST_CP850 = [
    "Relatório de vendas.docx",
    "Informação geral.pdf",
    "Søren Møde notater.docx",
    "Álbum de família/São Paulo.jpg",
    "Brønnøysund Øvelse.txt",
]

WEST_CP1252 = [
    "Résumé Müller.docx",
    "Señor Niño – Fotos.jpg",
    "Über uns/Straße.txt",
    "Côte d’Azur été 2023.jpg",
    "naïve façade.txt",
]

# code page -> names that the code page can represent
CORPUS: dict[str, list[str]] = {
    "cp437": WEST_CP437,
    "cp850": WEST_CP850,
    "cp1252": WEST_CP1252,
    "cp1256": ARABIC,
    "cp720": ARABIC,
    "cp866": RUSSIAN,
    "cp1251": RUSSIAN,
    "cp1253": GREEK,
    "cp1255": HEBREW,
    "cp932": JAPANESE,
    "gbk": SIMPLIFIED,
    "big5": TRADITIONAL,
    "cp949": KOREAN,
}


def legacy_zip(path: Path, names: list[str], encoding: str, *, flags: int = 0) -> Path:
    """A ZIP whose names are stored as raw ``encoding`` bytes, without the UTF-8 flag."""
    entries = [
        file_entry(name.encode(encoding), f"content of {i}".encode(), flags=flags)
        for i, name in enumerate(names)
    ]
    return write_raw_zip(path, entries)


def extracted_files(root: Path) -> list[str]:
    """Relative POSIX paths of every file below ``root``, ignoring the resume journal."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".zipwise-resume.json")
    )
