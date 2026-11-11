"""Archive file-name encoding detection.

ZIP files written by old tools store names in the writer's OEM or ANSI code page without
saying which. :func:`detect_names` decides per archive, over the whole list of names:

1. all names are strict UTF-8 and at least one has a multi-byte sequence -> ``utf-8``;
2. pure ASCII -> ``ascii``;
3. otherwise every candidate code page that decodes all names is scored by how much the
   result looks like real file names (coherent script, plausible letters, no box drawing,
   no private-use or control characters) and the best one wins;
4. a locale hint only decides between candidates that are practically tied.
"""

from __future__ import annotations

import functools
import locale
import math
import unicodedata
from collections import Counter
from dataclasses import dataclass

CANDIDATES: tuple[str, ...] = (
    "cp437",
    "cp850",
    "cp1252",
    "cp1256",
    "cp720",
    "cp866",
    "cp1251",
    "cp1253",
    "cp1255",
    "cp932",
    "gbk",
    "big5",
    "cp949",
)

_MAX_SAMPLE = 2000
_FALLBACK_PENALTY = -3.0

_LOCALE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "ar": ("cp1256", "cp720"),
    "fa": ("cp1256",),
    "ur": ("cp1256",),
    "ru": ("cp866", "cp1251"),
    "uk": ("cp866", "cp1251"),
    "bg": ("cp866", "cp1251"),
    "be": ("cp866", "cp1251"),
    "el": ("cp1253",),
    "he": ("cp1255",),
    "ja": ("cp932",),
    "ko": ("cp949",),
    "zh_cn": ("gbk",),
    "zh_sg": ("gbk",),
    "zh_tw": ("big5",),
    "zh_hk": ("big5",),
    "zh": ("gbk", "big5"),
    "en": ("cp437", "cp850", "cp1252"),
    "de": ("cp850", "cp437", "cp1252"),
    "fr": ("cp850", "cp437", "cp1252"),
    "es": ("cp850", "cp437", "cp1252"),
    "pt": ("cp850", "cp437", "cp1252"),
}


@dataclass(frozen=True)
class EncodingGuess:
    encoding: str
    confidence: float
    alternatives: tuple[str, ...] = ()


# --- reference data -------------------------------------------------------------------------

_CYR_COMMON = "оеаинтсрвлкмдпуяыьгзбчй"
_CYR_MID = "хжшю"
_CYR_RARE = "цщэф"
_CYR_VERY_RARE = "ъ"
_CYR_UKR = "іїєґ"
_CYR_RARE_PENALISED = "ЂЃЉЊЌЋЏђѓљњќћџЎўЈјҐѕЅѐЀѝЍ"
_GREEK_COMMON = "αεηιοντρσςκυμπλ"
_ARABIC_COMMON = "الیمنوترعبدهةسكفقحجشصأيى"
_ARABIC_OTHER = "ءآإؤئطظثذزغضخ"
_ARABIC_PERSIAN = "پچژگکیںھہے"
_HEBREW_COMMON = "יוהלארתמשנב"
_HEBREW_OTHER = "דחעכקפס"
_HEBREW_RARE = "גצזט"
_HEBREW_FINAL = "ךםןףץ"
_LATIN_COMMON_ACCENTED = "àáâãäåçèéêëìíîïñòóôõöøùúûüýÿß"
_LATIN_ODD = "æðþœŋƒĳ"

_COMMON_HAN = (
    "的一是不了人我在有他这为之大来以个中上们到说国和地也子时道出而要于就下得可你年生自会"
    "那后能对着事其里所去行过家十用发天如然作方成者多日都三小军二无同么经法当起与好看学进"
    "种将还分此心前面又定见只主没公从报告销售文件资料图片照片视频音乐工作项目计划总结会议"
    "记录报表财务合同发票客户产品设计开发测试数据管理系统用户信息网络电脑软件程序代码备份"
    "下载新建文档表格演示稿相册旅行家庭生日春节假期学习课程作业论文笔记考试成绩简历申请通"
    "知公告说明手册指南教程方案预算统计分析结果月份季度全年第四五六七八九百千万元号名称类"
    "型大小修改期最新版本使用运营市场部门人员员工会计审核批准采购订单付款收入支出利润成本"
    "的一是不了人我在有他這為之大來以個中上們到說國和地也子時道出而要於就下得可你年生自會"
    "那後能對著事其裡所去行過家十用發天如然作方成者多日都三小軍二無同麼經法當起與好看學進"
    "種將還分此心前面又定見只主沒公從報告銷售檔案資料圖片照片影片音樂專案計畫總結會議記錄"
    "報表財務合約發票客戶產品設計開發測試管理系統使用者資訊網路電腦軟體程式備份下載新建文件"
    "簡報相簿旅行家庭生日春節假期學習課程作業論文筆記考試成績履歷申請通知公告說明手冊指南教"
    "學方案預算統計分析結果月份季度全年第號稱類型大小修改日期最新版本運營市場部門人員員工"
    "會計審核批准採購訂單付款收入支出利潤成本"
    "売上報告書家族写真旅行議事録会社資料契約請求見積開発設計図面画像音楽映画仕事予定表計画"
    "総括会議経理発注納品顧客商品試験管理利用者情報電子機器集卒業式誕夏休冬課題宿題論文成績"
    "履歴届出案内説明取扱手順例統分析結果東京大阪日本語中国韓国人名前写真館図書館学校先生"
)

_HANGUL_COMMON = (
    "가각간감강개객거건검게격견결경계고곡공과관광교구국군권규그근금급기길김나날남내년노논"
    "농높능다단달당대더던도독동되두드들등디라락란랑래량러력련로록론료루류률리린림마만말맛"
    "매면명모목무문물미민바박반받발방배백번법변별병보복본부분불비사산상새생서선설성세소속"
    "수순술스시식신실심아안알압애약양어업에여역연열영예오온완외요용우운원월위유육은을음응"
    "의이인일임입자작잔장재전점정제조종좋주중증지직진질집차참창채책처천철첫청체초최추출충"
    "취치친카코크타태택터테토통투트특파판패페편평포표품프피필하학한할함합항해행향현형호화"
    "확환활회후훈휴희뉴랜맞맡멋멀며몸몰빠빛뼈뿐쓰씨씩앞앙얼얻엄엇엔엘엠열옛옷와왕왜욕웃워"
    "원웹윈익잎잘잠잡재쟁저적절접젊제져존졸죽준줄즉즐지짓짜쪽찬찾천첨초촌총촬축춘취측층치"
)

_PUNCT_OK = "–—‘’“”«»•·…"
_SYMBOL_OK = "°±×÷§©®™€£¥№"
_BAD_SYMBOL = -1.2
_BAD_BOX = -2.2
_BAD_CONTROL = -4.0


@functools.cache
def _common_han() -> frozenset[str]:
    return frozenset(_COMMON_HAN)


@functools.cache
def _hangul_common() -> frozenset[str]:
    return frozenset(_HANGUL_COMMON)


def _decode_pairs(codec: str, leads: range, trails: tuple[range, ...]) -> frozenset[str]:
    found: set[str] = set()
    for lead in leads:
        for trail_range in trails:
            for trail in trail_range:
                try:
                    found.add(bytes((lead, trail)).decode(codec))
                except UnicodeDecodeError:
                    continue
    return frozenset(found)


@functools.cache
def _level1_han() -> frozenset[str]:
    """Common-ish ideographs: the first (frequency-ordered) levels of GB2312, Big5, JIS X 0208."""
    gb = _decode_pairs("gb2312", range(0xB0, 0xD8), (range(0xA1, 0xFF),))
    big5 = _decode_pairs("big5", range(0xA4, 0xC7), (range(0x40, 0x7F), range(0xA1, 0xFF)))
    jis = _decode_pairs("cp932", range(0x88, 0x99), (range(0x40, 0x7F), range(0x80, 0xFD)))
    return gb | big5 | jis


@functools.cache
def _ksx1001_hangul() -> frozenset[str]:
    return _decode_pairs("cp949", range(0xB0, 0xC9), (range(0xA1, 0xFF),))


# --- character classification ---------------------------------------------------------------

_FAMILY_BY_PREFIX = {
    "LATIN": "latin",
    "ARABIC": "arabic",
    "CYRILLIC": "cyrillic",
    "GREEK": "greek",
    "HEBREW": "hebrew",
    "CJK": "cjk",
    "HIRAGANA": "cjk",
    "KATAKANA": "cjk",
    "HANGUL": "hangul",
}


@functools.lru_cache(maxsize=65536)
def _family(ch: str) -> str:
    if ch < "\x80":
        return "latin"
    try:
        prefix = unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return "other"
    if prefix == "HALFWIDTH" or prefix == "FULLWIDTH":
        return "cjk"
    # Ligatures such as "LATIN SMALL LIGATURE" and "SUPERSCRIPT LATIN SMALL LETTER" are rare
    # anyway; anything not matched here counts as its own odd script.
    return _FAMILY_BY_PREFIX.get(prefix, "other")


def _char_value(ch: str, *, word_start: bool, prev_lower: bool) -> tuple[float, str]:
    """Plausibility of one non-ASCII character as part of a file name, and its script family.

    Roughly +1 for a very common letter, 0 for neutral, negative for things that do not
    occur in real names.
    """
    cat = unicodedata.category(ch)
    code = ord(ch)
    if cat in ("Cn", "Co", "Cs"):
        return _BAD_CONTROL, "other"
    if cat in ("Cc", "Cf"):
        return _BAD_CONTROL if cat == "Cc" else -1.5, "other"
    if 0x2500 <= code <= 0x25FF:
        return _BAD_BOX, "other"
    if 0xFF61 <= code <= 0xFF9F:
        return -0.9, "cjk"  # half-width katakana: what stray high bytes become in Shift-JIS
    family = _family(ch)
    if cat[0] == "L":
        return _letter_value(ch, family, cat, word_start, prev_lower), family
    if cat[0] == "M":
        if family == "other" and 0x064B <= code <= 0x0652:
            family = "arabic"
        if family == "arabic":
            return (0.0 if not word_start else -1.0), "arabic"
        return -0.6, family if family != "other" else "latin"
    if cat == "Nd":
        return 0.3, "other"
    if cat[0] == "P":
        if ch in _PUNCT_OK:
            return -0.1, "other"
        if 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF or ch in "،؛؟":
            return 0.3, "other"
        return -0.5, "other"
    if cat == "Zs":
        return (0.2 if code == 0x3000 else -0.8), "other"
    if cat[0] == "S" or cat in ("No", "Nl"):
        if ch in _SYMBOL_OK:
            return -0.4, "other"
        if 0xFF00 <= code <= 0xFFEF:
            return 0.3, "other"
        return _BAD_SYMBOL, "other"
    return -1.0, "other"


def _letter_value(ch: str, family: str, cat: str, word_start: bool, prev_lower: bool) -> float:
    lower = ch.lower()
    upper = cat == "Lu"
    mid_cap = upper and prev_lower
    if family == "latin":
        if cat == "Lm":
            return -1.2
        base = unicodedata.normalize("NFD", ch)[0]
        if lower in _LATIN_ODD:
            return 0.0
        if lower in _LATIN_COMMON_ACCENTED:
            if mid_cap:
                return -1.0
            return 0.3 if upper else 0.6
        if base.isascii():
            return -0.8 if mid_cap else 0.1
        return -0.5 if mid_cap else 0.0
    if family == "cyrillic":
        if ch in _CYR_RARE_PENALISED:
            return -1.5
        if mid_cap:
            return -1.5
        if lower in _CYR_COMMON:
            return 0.95 if not upper else 0.6
        if lower in _CYR_MID:
            return 0.6 if not upper else 0.4
        if lower in _CYR_RARE:
            return 0.35 if not upper else 0.2
        if lower in _CYR_VERY_RARE:
            return -0.2
        if lower in ("ё",):
            return 0.2 if not upper else 0.0
        if lower in _CYR_UKR:
            return 0.35
        return -0.5
    if family == "greek":
        if mid_cap:
            return -1.5
        if lower in _GREEK_COMMON:
            return 0.95 if not upper else 0.6
        return 0.7 if not upper else 0.5
    if family == "arabic":
        if ch in _ARABIC_COMMON:
            return 0.95
        if ch in _ARABIC_OTHER:
            return 0.8
        if ch in _ARABIC_PERSIAN:
            return 0.15
        if ch == "ـ":
            return 0.3
        return -0.3
    if family == "hebrew":
        if ch in _HEBREW_COMMON:
            return 0.95
        if ch in _HEBREW_OTHER:
            return 0.8
        if ch in _HEBREW_RARE:
            return 0.6
        if ch in _HEBREW_FINAL:
            return 0.5
        return 0.0
    if family == "cjk":
        return _cjk_value(ch)
    if family == "hangul":
        if ch in _hangul_common():
            return 1.0
        if ch in _ksx1001_hangul():
            return 0.35
        return 0.1
    return -0.5


def _cjk_value(ch: str) -> float:
    code = ord(ch)
    if 0xE000 <= code <= 0xF8FF:
        return _BAD_CONTROL
    if 0x3040 <= code <= 0x309F or 0x30A0 <= code <= 0x30FF:
        return 0.95  # kana
    if 0xFF00 <= code <= 0xFFEF:
        return 0.3
    if ch in _common_han():
        return 1.0
    if ch in _level1_han():
        return 0.5
    if 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF or code >= 0x20000:
        return -0.3
    return 0.1


def _is_lower(ch: str) -> bool:
    return unicodedata.category(ch) == "Ll"


# --- scoring --------------------------------------------------------------------------------


def _byte_len(ch: str, encoding: str, single_byte: bool) -> int:
    if single_byte or ch < "\x80":
        return 1
    try:
        return max(1, len(ch.encode(encoding)))
    except UnicodeEncodeError:
        return 2


def _flush_word(word: list[tuple[str, int, str]], accented: int, letters: int) -> float:
    """Word-level adjustments: mixed scripts, and Latin words made mostly of accents."""
    if not word:
        return 0.0
    penalty = 0.0
    counts: Counter[str] = Counter()
    weight: Counter[str] = Counter()
    for index, (family, nbytes, ch) in enumerate(word):
        counts[family] += 1
        weight[family] += nbytes
        if ch in _HEBREW_FINAL and index < len(word) - 1 and word[index + 1][0] == "hebrew":
            penalty -= 1.5  # final-form letters only occur at the end of a word
    meaningful = {f: n for f, n in counts.items() if f != "other"}
    if len(meaningful) > 1:
        dominant = max(meaningful, key=lambda f: meaningful[f])
        for family in meaningful:
            if family == dominant:
                continue
            soft = dominant in ("cjk", "hangul") and family == "latin"
            penalty -= (0.15 if soft else 1.5) * weight[family]
        if "hangul" in meaningful and "cjk" in meaningful:
            penalty -= 1.0 * weight["cjk"]
    if letters >= 4 and "latin" in meaningful:
        ratio = accented / letters
        if ratio > 0.5:
            penalty -= 0.9 * accented
        elif ratio > 0.34:
            penalty -= 0.4 * accented
    return penalty


def _score_text(text: str, encoding: str, single_byte: bool) -> float:
    total = 0.0
    word: list[tuple[str, int, str]] = []
    accented = letters = 0
    prev = ""
    run = 0
    for ch in text:
        is_letter = ch.isalpha() or (ch >= "\x80" and unicodedata.category(ch)[0] == "M")
        if ch < "\x80":
            if is_letter:
                word.append(("latin", 1, ch))
                letters += 1
            else:
                total += _flush_word(word, accented, letters)
                word, accented, letters = [], 0, 0
            run = run + 1 if ch == prev else 1
            if run >= 4 and ch.isalpha():
                total -= 0.5
            prev = ch
            continue
        nbytes = _byte_len(ch, encoding, single_byte)
        word_start = not word
        prev_lower = bool(prev) and _is_lower(prev)
        value, family = _char_value(ch, word_start=word_start, prev_lower=prev_lower)
        total += value * nbytes
        if is_letter:
            word.append((family, nbytes, ch))
            letters += 1
            if family == "latin":
                accented += 1
        else:
            total += _flush_word(word, accented, letters)
            word, accented, letters = [], 0, 0
        run = run + 1 if ch == prev else 1
        if run >= 3:
            total -= 1.0
        prev = ch
    total += _flush_word(word, accented, letters)
    return total


@functools.cache
def _is_single_byte(encoding: str) -> bool:
    return encoding not in ("cp932", "gbk", "big5", "cp949", "utf-8")


@dataclass(frozen=True)
class _Scored:
    encoding: str
    score: float
    texts: tuple[str, ...]


def _non_ascii_bytes(raws: list[bytes]) -> int:
    return sum(1 for raw in raws for b in raw if b >= 0x80)


def _score_candidate(raws: list[bytes], encoding: str) -> _Scored | None:
    single = _is_single_byte(encoding)
    texts: list[str] = []
    score = 0.0
    for raw in raws:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            if encoding != "utf-8":
                return None
            score += _FALLBACK_PENALTY
            texts.append(raw.decode("cp437"))
            continue
        texts.append(text)
        score += _score_text(text, encoding, single)
    return _Scored(encoding, score, tuple(texts))


def _sample(raws: list[bytes]) -> list[bytes]:
    unique = sorted(set(raws))
    if len(unique) <= _MAX_SAMPLE:
        return unique
    step = len(unique) / _MAX_SAMPLE
    return [unique[int(i * step)] for i in range(_MAX_SAMPLE)]


def _hint_order(hint: str | None) -> tuple[str, ...]:
    if not hint:
        return ()
    text = hint.strip().lower().replace("-", "_").split(".")[0].split("@")[0]
    if text in CANDIDATES:
        return (text,)
    if text in _LOCALE_PREFERENCE:
        return _LOCALE_PREFERENCE[text]
    return _LOCALE_PREFERENCE.get(text.split("_")[0], ())


def system_locale_hint() -> str | None:
    """The user's locale (for example ``ar_EG``), usable as ``hint_locale``."""
    try:
        return locale.getlocale()[0]
    except (ValueError, TypeError):
        return None


def _is_strict_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def detect_names(raw_names: list[bytes], hint_locale: str | None = None) -> EncodingGuess:
    """Guess the encoding shared by ``raw_names``."""
    non_ascii = [raw for raw in raw_names if any(b >= 0x80 for b in raw)]
    if not non_ascii:
        return EncodingGuess("ascii", 1.0)
    valid = [raw for raw in non_ascii if _is_strict_utf8(raw)]
    if len(valid) == len(non_ascii):
        return EncodingGuess("utf-8", 0.99 if len(valid) > 2 else 0.95, ("cp437",))

    sample = _sample(non_ascii)
    candidates = list(CANDIDATES)
    if valid:
        candidates.insert(0, "utf-8")  # mixed archive: some names are UTF-8, some are not
    scored = [s for s in (_score_candidate(sample, enc) for enc in candidates) if s is not None]
    if not scored:
        return EncodingGuess("cp437", 0.05)

    best = max(scored, key=lambda s: s.score)  # first in candidate order wins exact ties
    tolerance = max(0.35, 0.03 * abs(best.score))
    tied = [s for s in scored if s.score >= best.score - tolerance]
    preferred = _hint_order(hint_locale)
    for enc in preferred:
        match = next((s for s in tied if s.encoding == enc), None)
        if match is not None:
            best = match
            break

    same_text = [s.encoding for s in scored if s is not best and s.texts == best.texts]
    others = sorted(
        (s for s in scored if s is not best and s.texts != best.texts),
        key=lambda s: -s.score,
    )
    nbytes = max(1, _non_ascii_bytes(sample))
    quality = max(0.0, min(1.0, best.score / nbytes))
    second = others[0].score if others else best.score - nbytes
    gap = max(0.0, min(1.0, (best.score - second) / max(abs(best.score), 1.0)))
    confidence = 0.05 if best.score <= 0 else 0.25 + 0.45 * quality + 0.3 * math.sqrt(gap)
    likely = tuple(s.encoding for s in others if s.score > 0.5 * best.score)[:3]
    alternatives = tuple(same_text) + likely
    return EncodingGuess(best.encoding, round(min(0.98, max(0.05, confidence)), 2), alternatives)


def decode_name(raw: bytes, encoding: str) -> str:
    """Decode one name; undecodable bytes become U+FFFD instead of failing the archive."""
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return raw.decode(encoding, errors="replace")


def decode_name_strict(raw: bytes, encoding: str) -> str | None:
    """Decode one name, or None when ``encoding`` cannot represent it."""
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return None
