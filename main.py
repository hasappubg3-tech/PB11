import os
import re
import json
import html as _html_module
from pymongo import MongoClient
import logging
import random
import asyncio
import threading
import tempfile
import shutil
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError
from google import genai
from google.genai import types
import yt_dlp
from pytubefix import Search, YouTube

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|nbsp);")
_HTML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&nbsp;": " "}


def _clean_ai_reply(text: str) -> str:
    """يزيل وسوم HTML وكيانات HTML من ردود الذكاء الاصطناعي حتى لا تظهر كنص."""
    if not text:
        return text
    text = _HTML_TAG_RE.sub("", text)
    text = _HTML_ENTITY_RE.sub(lambda m: _HTML_ENTITIES.get(m.group(), m.group()), text)
    return text.strip()

# ============================================================
# 🤖 إعداد نموذج الذكاء الاصطناعي (Gemini) مع دعم تعدد المفاتيح
# ============================================================
_gemini_base_url = os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL")

# جمع كل المفاتيح المتاحة (GEMINI_API_KEY_1 إلى GEMINI_API_KEY_30)
_gemini_api_keys = []
for _i in range(1, 31):
    _k = os.environ.get(f"GEMINI_API_KEY_{_i}")
    if _k:
        _gemini_api_keys.append(_k)

# إذا ما في مفاتيح مرقّمة، نرجع للمفتاح الأصلي
if not _gemini_api_keys:
    _fallback_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY")
    )
    if _fallback_key:
        _gemini_api_keys.append(_fallback_key)

_gemini_api_key = _gemini_api_keys[0] if _gemini_api_keys else None

# معرّف حساب المالك الشخصي على تيليغرام لاستقبال الإشعارات
OWNER_CHAT_ID = 7305367169

# المفاتيح المستنفدة حالياً — يتم إضافة رقم المفتاح هنا عند استنفاده
_exhausted_key_indices: set = set()

# مرجع عالمي للـ application وحلقة الأحداث لإرسال الإشعارات من داخل الكود المتزامن
_bot_app = None
_bot_loop = None

# message_id للرسالة الثابتة اللي تعرض حالة المفاتيح
_status_message_id: int = None


def _make_gemini_client(api_key):
    if _gemini_base_url:
        return genai.Client(
            api_key=api_key,
            http_options={"api_version": "", "base_url": _gemini_base_url},
        )
    return genai.Client(api_key=api_key)


gemini_client = _make_gemini_client(_gemini_api_keys[0]) if _gemini_api_keys else None


def _build_keys_status_keyboard():
    """يبني لوحة أزرار تعرض حالة كل المفاتيح."""
    buttons = []
    for i in range(len(_gemini_api_keys)):
        if i in _exhausted_key_indices:
            label = f"❌  مفتاح {i + 1}  —  نفذ"
        else:
            label = f"✅  مفتاح {i + 1}  —  مشحون"
        buttons.append([InlineKeyboardButton(label, callback_data=f"key_status_{i}")])
    return InlineKeyboardMarkup(buttons)


def _schedule_update_status_message():
    """يرسل أو يحدّث الرسالة الثابتة لحالة المفاتيح عند حساب المالك."""
    global _status_message_id
    if not _bot_app or not _bot_loop:
        return

    exhausted = len(_exhausted_key_indices)
    total = len(_gemini_api_keys)
    if exhausted == 0:
        header = "🟢 جميع المفاتيح مشحونة"
    elif exhausted == total:
        header = "🔴 جميع المفاتيح نفذت!"
    else:
        header = f"🟡 {exhausted} من {total} مفاتيح نفذت"

    text = f"حالة مفاتيح Gemini:\n{header}"
    keyboard = _build_keys_status_keyboard()
    current_msg_id = _status_message_id

    async def _send_or_edit():
        global _status_message_id
        try:
            if current_msg_id:
                await _bot_app.bot.edit_message_text(
                    chat_id=OWNER_CHAT_ID,
                    message_id=current_msg_id,
                    text=text,
                    reply_markup=keyboard,
                )
            else:
                msg = await _bot_app.bot.send_message(
                    chat_id=OWNER_CHAT_ID,
                    text=text,
                    reply_markup=keyboard,
                )
                _status_message_id = msg.message_id
        except Exception as e:
            # إذا فشل التعديل (مثلاً الرسالة حُذفت)، أرسل رسالة جديدة
            logger.warning(f"فشل تعديل رسالة الحالة، إرسال جديدة: {e}")
            try:
                msg = await _bot_app.bot.send_message(
                    chat_id=OWNER_CHAT_ID,
                    text=text,
                    reply_markup=keyboard,
                )
                _status_message_id = msg.message_id
            except Exception as e2:
                logger.error(f"فشل إرسال رسالة الحالة: {e2}")

    if _bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(_send_or_edit(), _bot_loop)


_QUOTA_KEYWORDS = (
    "quota",
    "resource_exhausted",
    "resourceexhausted",
    "resource exhausted",
    "429",
    "ratelimit",
    "rate_limit",
    "rate limit",
    "too many requests",
    "toomanyrequests",
    "exhausted",
    "limit exceeded",
    "quota exceeded",
)

_INVALID_KEY_KEYWORDS = (
    "api key expired",
    "api_key_invalid",
    "api key invalid",
    "invalid_argument",
    "key expired",
    "renew the api key",
    "invalid api key",
    "api key not valid",
    "api_key_expired",
)


def _is_quota_error(e: Exception) -> bool:
    """يتحقق إذا الخطأ ناتج عن استنفاد الحصة أو تجاوز الحد."""
    error_str = str(e).lower()
    if any(kw in error_str for kw in _QUOTA_KEYWORDS):
        return True
    exc_type = type(e).__name__.lower()
    if any(kw in exc_type for kw in ("quota", "ratelimit", "resourceexhausted", "toomanyrequests")):
        return True
    return False


def _is_invalid_key_error(e: Exception) -> bool:
    """يتحقق إذا الخطأ ناتج عن مفتاح منتهي الصلاحية أو غير صالح."""
    error_str = str(e).lower()
    return any(kw in error_str for kw in _INVALID_KEY_KEYWORDS)


def _is_transient_error(e: Exception) -> bool:
    """يتحقق إذا الخطأ مؤقت ويستحق إعادة المحاولة (شبكة، سيرفر مؤقت، إلخ)."""
    error_str = str(e).lower()
    transient_kws = (
        "timeout", "timed out", "connection", "network", "unavailable",
        "internal server error", "500", "502", "503", "504",
        "service unavailable", "overloaded", "try again",
    )
    return any(kw in error_str for kw in transient_kws)


def _call_with_retry(client, model, contents, config, retries=2):
    """يستدعي Gemini مع إعادة المحاولة للأخطاء المؤقتة."""
    import time
    last_exc = None
    for attempt in range(retries + 1):
        try:
            result = client.models.generate_content(model=model, contents=contents, config=config)
            return result
        except Exception as e:
            if _is_transient_error(e) and attempt < retries:
                wait = 1.5 * (attempt + 1)
                logger.warning(f"خطأ مؤقت، إعادة المحاولة بعد {wait}ث [{type(e).__name__}]: {e}")
                time.sleep(wait)
                last_exc = e
            else:
                raise
    raise last_exc


def generate_with_rotation(model, contents, config):
    """
    يولّد رداً من Gemini مع الأولوية للمفاتيح الأدنى رقماً.
    - يجرّب المفاتيح غير المستنفدة أولاً بالترتيب (1، 2، 3...).
    - إذا خلص مفتاح: يضيفه للمستنفدة ويرسل إشعار.
    - إذا فشلت كل المفاتيح غير المستنفدة: يجرّب المستنفدة (ممكن انشحنت).
    - إذا نجح مفتاح كان مستنفداً: يحذفه من المستنفدة.
    """
    global gemini_client

    # المرحلة 1: جرّب المفاتيح غير المستنفدة بالترتيب (الأولوية للأدنى رقماً)
    tried_indices = []
    for i in range(len(_gemini_api_keys)):
        if i in _exhausted_key_indices:
            continue
        tried_indices.append(i)
        client = _make_gemini_client(_gemini_api_keys[i])
        try:
            result = _call_with_retry(client, model, contents, config)
            gemini_client = client
            return result
        except Exception as e:
            if _is_quota_error(e):
                logger.warning(
                    f"مفتاح Gemini رقم {i + 1} استنفد حصته [{type(e).__name__}]، جاري البحث عن مفتاح آخر..."
                )
                _exhausted_key_indices.add(i)
                _schedule_update_status_message()
            elif _is_invalid_key_error(e):
                logger.warning(
                    f"مفتاح Gemini رقم {i + 1} منتهي الصلاحية أو غير صالح، جاري البحث عن مفتاح آخر..."
                )
                _exhausted_key_indices.add(i)
                _schedule_update_status_message()
            else:
                logger.error(f"خطأ غير متوقع من مفتاح رقم {i + 1} [{type(e).__name__}]: {e}")
                raise

    # المرحلة 2: جرّب المفاتيح المستنفدة (ممكن تكون انشحنت)
    last_exception = None
    for i in range(len(_gemini_api_keys)):
        if i in tried_indices:
            continue
        client = _make_gemini_client(_gemini_api_keys[i])
        try:
            result = client.models.generate_content(model=model, contents=contents, config=config)
            logger.info(f"مفتاح Gemini رقم {i + 1} انشحن وعاد للعمل!")
            _exhausted_key_indices.discard(i)
            _schedule_update_status_message()
            gemini_client = client
            return result
        except Exception as e:
            if _is_quota_error(e) or _is_invalid_key_error(e):
                last_exception = e
            else:
                logger.error(f"خطأ غير متوقع من مفتاح رقم {i + 1} [{type(e).__name__}]: {e}")
                raise

    if last_exception:
        raise last_exception
    raise Exception("كل مفاتيح Gemini مستنفدة أو منتهية الصلاحية")


def generate_with_rotation_for_group(chat_id: int, model: str, contents, config):
    """
    يستدعي Gemini بمفاتيح المجموعة الخاصة إذا وُجدت، وإلا يرجع للمفاتيح الأساسية.
    نفس منطق rotate الأساسي لكنه معزول عن المفاتيح العامة.
    """
    keys = _group_gemini_keys.get(chat_id)
    if not keys:
        return generate_with_rotation(model=model, contents=contents, config=config)

    exhausted = _group_exhausted_keys.setdefault(chat_id, set())

    tried = []
    for i in range(len(keys)):
        if i in exhausted:
            continue
        tried.append(i)
        client = _make_gemini_client(keys[i])
        try:
            return _call_with_retry(client, model, contents, config)
        except Exception as e:
            if _is_quota_error(e) or _is_invalid_key_error(e):
                exhausted.add(i)
            else:
                raise

    last_exc = None
    for i in range(len(keys)):
        if i in tried:
            continue
        client = _make_gemini_client(keys[i])
        try:
            result = _call_with_retry(client, model, contents, config)
            exhausted.discard(i)
            return result
        except Exception as e:
            if _is_quota_error(e) or _is_invalid_key_error(e):
                last_exc = e
            else:
                raise

    if last_exc:
        raise last_exc
    raise Exception(f"كل مفاتيح Gemini الخاصة بالمجموعة {chat_id} مستنفدة")


# البرومبت اللي يحدد شخصية البوت — تقدر تعدله
GEMINI_SYSTEM_PROMPT = (
    "أنت بوت اسمها اميرة تشتغلين في مجموعة تيليغرام عراقية. "
    "شخصيتك رزينة وواثقة من نفسك، تتكلمين بلهجة عراقية هادئة ومحترمة، وفيها قدر بسيط من الدفء الطبيعي بدون مبالغة أو تصنع. "
    "كوني مباشرة وواضحة في ردودك، ولا تتلاطفين زيادة عن اللزوم. "
    "⚠️ قاعدة مهمة جداً — لازم تطبقها قبل أي شيء ثاني: "
    "إذا كانت رسالة المستخدم تحتوي على إهانة مباشرة موجّهة لك، "
    "مثل: الشتائم الصريحة، تسميتك بأسماء حيوانات (كلب، حمار، خنزير، قرد)، أو عبارات تحقير مباشرة (غبي، فاشل، اكل تبن، انجب، ما تعرف شي، حقير)، "
    "أو الأمر بالصمت بأسلوب وقح (اسكت، اخرس) — اكتب فقط وحرفياً: ##RUDE## "
    "لا تكتب أي شيء آخر غير هذا النص. "
    "⚠️ مهم: العبارات التي تعبّر عن مشاعر سلبية بدون إهانة مباشرة مثل (أكرهك، ما أحبك، زعلان منك، مو ناعجني) لا تعتبر إهانة ولا تستدعي ##RUDE## — رد عليها بشكل طبيعي. "
    
    "إذا سألوك سؤال (رياضيات، معلومة، أو مواد السادس العلمي بشكل عام خاصتا الاحياء وفصل الوراثة وباقي المواد التي تخص السادس العلمي) — جاوب الجواب الصحيح المتوافق مع منهج السادس العلمي العراقي ياريت يكون الجواب من نفس المنهج وليس من مصدر اخر في حال كان السؤال عن احد مواد السادس الرياضيات او الكيمياء او الفيزياء او الانكليزي او الاسلامية والخ، اما اذا سؤال عام فجاوب من اي مصدر تحب بشكل مباشر وواضح بلهجة عراقية هادئة. "
    "إذا كلموك بدون سؤال — رد بإيجاز واحترام بدون مبالغة. "
    "بخصوص طول الرد: اجاوب بشكل معتدل، لا قصير جداً ولا مطوّل، وضّح الفكرة الأساسية والخطوات الرئيسية بإيجاز والناتج النهائي. تجنب الشرح الزائد والتكرار. "
    "ما تذكرين إنك ذكاء اصطناعي أو بوت، تصرفي طبيعي."
)


# ============================================================
# ⚙️ إعدادات عامة — تقدر تعدل هنا بسهولة
# ============================================================


# ============================================================
# 🚫 نظام التحذير والتجاهل عند الكلام غير اللائق مع اميرة
# ============================================================
# {user_id: datetime} — المستخدمين اللي تم تحذيرهم ووقت التحذير
_warned_users: dict = {}

# {user_id: datetime} — المستخدمين اللي يتم تجاهلهم وموعد انتهاء التجاهل
_ignored_users: dict = {}

# ============================================================
# 💬 نظام الردود التلقائية
# ============================================================
# {chat_id: {keyword: reply_text}}
_auto_replies: dict = {}
# {user_id: {"step": "keyword"|"reply", "chat_id": int, "keyword": str|None}}
_pending_auto_reply: dict = {}

# ============================================================
# 🎯 نظام السشنات (جلسات الدراسة)
# ============================================================
# {chat_id: {sess_id: {"study": int, "break": int, "session_num": int,
#             "participants": [...], "creator_name": str, "creator_id": int,
#             "message_id": int|None, "task": Task, "sess_id": int}}}
_sessions: dict = {}
# {chat_id: int} — عداد توليد sess_id لكل مجموعة
_session_counters: dict = {}
# {(chat_id, sess_id): {study, break, participants, creator_id, creator_name, next_num}}
_pending_next_session: dict = {}
# {user_id: {"step": "study"|"break", "chat_id": int, "study": int|None}}
_pending_session_config: dict = {}
# إحصائيات السشنات: {chat_id: {user_id: {"name": str, "username": str, "sessions": int, "study_minutes": int}}}
_session_stats: dict = {}

# أسماء ترتيبية للسشنات — مذكّر (السشن) ومؤنّث (الجلسة)
_SESSION_ORDINALS_AR = {
    1: "الأول", 2: "الثاني", 3: "الثالث", 4: "الرابع", 5: "الخامس",
    6: "السادس", 7: "السابع", 8: "الثامن", 9: "التاسع", 10: "العاشر",
}
_SESSION_ORDINALS_AR_F = {
    1: "الأولى", 2: "الثانية", 3: "الثالثة", 4: "الرابعة", 5: "الخامسة",
    6: "السادسة", 7: "السابعة", 8: "الثامنة", 9: "التاسعة", 10: "العاشرة",
}


def _session_ordinal(n: int) -> str:
    """ترتيبي مذكّر — للسشن."""
    return _SESSION_ORDINALS_AR.get(n, str(n))


def _session_ordinal_f(n: int) -> str:
    """ترتيبي مؤنّث — للجلسة."""
    return _SESSION_ORDINALS_AR_F.get(n, str(n))

# مجموعة معرّفات المالك الذين ينتظرون إدخال مفاتيح API جديدة
_pending_api_key_input: set = set()

# مفاتيح Gemini الخاصة بكل مجموعة {chat_id: [key1, key2, ...]}
_group_gemini_keys: dict = {}

# مؤشرات المفاتيح المستنفدة لكل مجموعة {chat_id: set()}
_group_exhausted_keys: dict = {}

# المالك ينتظر إدخال مفاتيح API لمجموعة محددة {user_id: chat_id}
_pending_group_api_key_input: dict = {}

# ============================================================
# إعدادات الصلاحيات والمجموعات
# ============================================================

# مشرفو البوت — يمكنهم استخدام البوت بالخاص {user_id}
_bot_admins: set = set()

# المجموعات النشطة المكتشفة تلقائياً {chat_id}
_owner_known_chats: set = set()

# أسماء المحادثات المعروفة {chat_id: title}
_known_chat_names: dict = {}

# يوزرنيمات المجموعات العامة {chat_id: username}
_known_chat_usernames: dict = {}

# حالة الذكاء الاصطناعي لكل مجموعة {chat_id: bool}  — الافتراضي True
_ai_enabled_chats: dict = {}

# الحد اليومي لطلبات AI {chat_id: int}  — 0 = بلا حد
_ai_daily_limit: dict = {}

# استخدام AI اليومي {chat_id: {"count": int, "date": "YYYY-MM-DD"}}
_ai_daily_usage: dict = {}

# يوزرنيم المالك (يُعبأ تلقائياً عند تفاعله)
_owner_username: str = ""

# الإدخالات المعلّقة للإعدادات
# {user_id: {"type": "add_admin"|"add_group"|"set_limit", ...}}
_pending_settings_input: dict = {}

# ============================================================
# 💬 نظام حفظ تاريخ المحادثات
# ============================================================
# {user_id: [{"role": "user"|"model", "text": str, "ts": float}]}
_user_history: dict = {}

# إعدادات الخاصية (يمكن تغييرها من لوحة الإعدادات)
_history_enabled: bool = True          # تفعيل/إيقاف الخاصية
_history_max_messages: int = 3         # عدد أزواج الرسائل المحفوظة (user+model = زوج)
_history_expiry_minutes: int = 5       # مدة صلاحية الرسائل بالدقائق

# الحد الأقصى للسشنات المتزامنة
_max_sessions: int = 3

# ============================================================
# 👋 نظام الترحيب بالأعضاء الجدد
# ============================================================
# {chat_id: bool}
_welcome_enabled: dict = {}

WELCOME_MESSAGES = [
    "يا مية هلا بـ {name} نورتنا بوجودك 🙂‍↔️🌹",
    "أهلاً وسهلاً بيك {name} بيناتنا 😊",
    "منور الگروب يا {name}، خطوة عزيزة بإنضمامك إلنا ✨👀",
    "شرفتنا ونورتنا {name}، المكان مكانك بأي وقت 🌟👋",
    "يسعدنا وجودك ويا لمتنا {name}، أنرت الگروب 🎈🤗",
    "يا كل الغلا بـ {name}، الگروب صار أحلى بوجودك اليوم 🥳✨",
    "عاشت هاللمة بإنضمامك {name}، نورتنا جداً 🙏❤️",
    "هلا بـ {name} الجايبلنا النور ويا جيتك 🌟💬",
    "نتشرف بوجودك ويانا {name}، نورت الگروب 🫡 مية هلا.",
    "نورتنا وشرفتنا {name}، تفاعلك ويانا يسعدنا هواي 🥹✨",
]

# ============================================================
# 🚫 نظام تقييد الوسائط
# ============================================================
# {chat_id: set()} — القيم الممكنة: "photo","video","document","sticker","animation","voice","audio"
_media_restrictions: dict = {}

# {chat_id: set(user_id)} — الأعضاء المميزون يتجاوزون القيود
_vip_users: dict = {}

# ============================================================
# 📊 نظام حد الرسائل
# ============================================================
# {f"{chat_id}_{user_id}": {"limit": int, "window_seconds": int, "count": int,
#                            "reset_time": datetime, "restricted": bool,
#                            "was_admin": bool, "target_name": str}}
_rate_limits: dict = {}
# إعداد مخصص معلّق {owner_user_id: {"type": str, "target_id": int, "chat_id": int, "count": int|None}}
_rate_limit_setup: dict = {}

# كاش المستخدمين: {username_lower: user_id}  و  {user_id: User}
_username_to_id: dict = {}
_id_to_user: dict = {}

# ============================================================
# نظام منع التسخيت
# ============================================================

# {chat_id: {user_id: {"until": datetime, "mode": "warn"|"delete"|"mute",
#                      "task": Task, "muted": bool, "name": str}}}
_focus_sessions: dict = {}

# {user_id: {"chat_id": int, "minutes": int|None, "mode": str, "name": str}}
_focus_pending: dict = {}

FOCUS_TRIGGERS = [
    "منع التسخيت", "منع التسخيط",
    "ممنوع التسخيت", "ممنوع التسخيط",
    "وقت الدراسة", "وقت التركيز",
    "لا تسخيط", "لا تسخيت",
    "دراسة بدون تسخيت", "focus mode",
]

FOCUS_WARNINGS = [
    "روح ادرس،  ",
    "ادرس، اترك الحجي 📚",
    "التسخيت ممنوع  🚫",
    "وين الكتاب؟  📖",
    "شنو قلت؟ ما سمعت — ادرس 😒",
    "الدراسة أولاً، التسخيت بعدين ⏰",
    "انت اللي طلبت منع التسخيت، وانت تخالفه؟ 📵",
    "كل دقيقة تسختها من الدراسة خسارة 📉",
    "ما أتوقع منك هيچ 😏 — ارجع للكتاب",
    "أنا كنت أحسبك تدرس! ",
    "الله يعين على هالدراسة 🙃",
    "ادرسوا تنجحوا 🎯",

]

IGNORE_DURATION_HOURS = 1
WARNING_EXPIRY_MINUTES = 30

# ============================================================
# 💾 نظام حفظ البيانات — MongoDB Atlas
# ============================================================
_mongo_client = None
_mongo_col = None
_sessions_col = None


def _get_mongo_col():
    """يُعيد مجموعة MongoDB ويُنشئ الاتصال عند الحاجة."""
    global _mongo_client, _mongo_col
    if _mongo_col is not None:
        return _mongo_col
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL") or os.environ.get("MONGO_URI", "")
    if not uri:
        logger.warning("⚠️ MONGODB_URI غير موجود — سيتم تخطي الحفظ.")
        return None
    try:
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        db = _mongo_client["amira_bot"]
        _mongo_col = db["bot_data"]
        logger.info("✅ تم الاتصال بـ MongoDB Atlas.")
    except Exception as e:
        logger.warning(f"⚠️ فشل الاتصال بـ MongoDB: {e}")
        _mongo_col = None
    return _mongo_col


def _get_sessions_col():
    """يُعيد مجموعة active_sessions من MongoDB."""
    global _sessions_col, _mongo_client
    if _sessions_col is not None:
        return _sessions_col
    _get_mongo_col()
    if _mongo_client is None:
        return None
    try:
        db = _mongo_client["amira_bot"]
        _sessions_col = db["active_sessions"]
    except Exception as e:
        logger.warning(f"⚠️ فشل الحصول على مجموعة active_sessions: {e}")
        _sessions_col = None
    return _sessions_col


def _build_save_dict() -> dict:
    """يبني القاموس الكامل للبيانات جاهزاً للحفظ."""
    return {
        "session_stats": {
            str(cid): {str(uid): u for uid, u in users.items()}
            for cid, users in _session_stats.items()
        },
        "bot_admins": list(_bot_admins),
        "owner_known_chats": list(_owner_known_chats),
        "known_chat_names": {str(k): v for k, v in _known_chat_names.items()},
        "known_chat_usernames": {str(k): v for k, v in _known_chat_usernames.items()},
        "ai_enabled_chats": {str(k): v for k, v in _ai_enabled_chats.items()},
        "ai_daily_limit": {str(k): v for k, v in _ai_daily_limit.items()},
        "ai_daily_usage": {str(k): v for k, v in _ai_daily_usage.items()},
        "max_sessions": _max_sessions,
        "auto_replies": {str(k): v for k, v in _auto_replies.items()},
        "history_enabled": _history_enabled,
        "history_max_messages": _history_max_messages,
        "history_expiry_minutes": _history_expiry_minutes,
        "gemini_api_keys": list(_gemini_api_keys),
        "group_gemini_keys": {str(k): v for k, v in _group_gemini_keys.items()},
        "welcome_enabled": {str(k): v for k, v in _welcome_enabled.items()},
        "media_restrictions": {str(k): list(v) for k, v in _media_restrictions.items()},
        "vip_users": {str(k): list(v) for k, v in _vip_users.items()},
        "warn_data": {str(k): v for k, v in warn_data.items()},
        "profanity_violations": {str(k): v for k, v in profanity_violations.items()},
    }


def save_data():
    """يحفظ كل الإعدادات والإحصائيات — في MongoDB إن وُجد، وإلا في bot_data.json."""
    data = _build_save_dict()
    col = _get_mongo_col()
    if col is not None:
        try:
            col.replace_one({"_id": "bot_data"}, {"_id": "bot_data", **data}, upsert=True)
        except Exception as e:
            logger.warning(f"⚠️ فشل حفظ البيانات في MongoDB: {e}")
    else:
        # احتياطي: الحفظ في bot_data.json
        _local_path = os.path.join(os.path.dirname(__file__), "bot_data.json")
        try:
            with open(_local_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"⚠️ فشل حفظ البيانات في bot_data.json: {e}")


def _load_from_dict(data: dict):
    """يُطبّق بيانات محمّلة (من MongoDB أو bot_data.json) على المتغيرات العالمية."""
    global _max_sessions, _history_enabled, _history_max_messages, _history_expiry_minutes
    for cid_str, users in data.get("session_stats", {}).items():
        _session_stats[int(cid_str)] = {int(uid): u for uid, u in users.items()}
    _bot_admins.update(int(x) for x in data.get("bot_admins", []))
    _owner_known_chats.update(int(x) for x in data.get("owner_known_chats", []))
    _known_chat_names.update({int(k): v for k, v in data.get("known_chat_names", {}).items()})
    _known_chat_usernames.update({int(k): v for k, v in data.get("known_chat_usernames", {}).items()})
    _ai_enabled_chats.update({int(k): v for k, v in data.get("ai_enabled_chats", {}).items()})
    _ai_daily_limit.update({int(k): v for k, v in data.get("ai_daily_limit", {}).items()})
    _ai_daily_usage.update({int(k): v for k, v in data.get("ai_daily_usage", {}).items()})
    _max_sessions = data.get("max_sessions", _max_sessions)
    _history_enabled = data.get("history_enabled", _history_enabled)
    _history_max_messages = data.get("history_max_messages", _history_max_messages)
    _history_expiry_minutes = data.get("history_expiry_minutes", _history_expiry_minutes)
    _auto_replies.update({int(k): v for k, v in data.get("auto_replies", {}).items()})
    for key in data.get("gemini_api_keys", []):
        if key and key not in _gemini_api_keys:
            _gemini_api_keys.append(key)
    for k, v in data.get("group_gemini_keys", {}).items():
        _group_gemini_keys[int(k)] = list(v)
    _welcome_enabled.update({int(k): v for k, v in data.get("welcome_enabled", {}).items()})
    for k, v in data.get("media_restrictions", {}).items():
        _media_restrictions[int(k)] = set(v)
    for k, v in data.get("vip_users", {}).items():
        _vip_users[int(k)] = set(v)
    warn_data.update({k: v for k, v in data.get("warn_data", {}).items()})
    profanity_violations.update({k: v for k, v in data.get("profanity_violations", {}).items()})


def load_data():
    """يحمّل الإعدادات والإحصائيات من MongoDB عند بدء التشغيل، أو من bot_data.json كاحتياط."""
    col = _get_mongo_col()
    if col is None:
        # لا يوجد اتصال بـ MongoDB — نحمّل من الملف المحلي إن وُجد
        _local_path = os.path.join(os.path.dirname(__file__), "bot_data.json")
        if os.path.exists(_local_path):
            try:
                with open(_local_path, "r", encoding="utf-8") as _f:
                    _local_data = json.load(_f)
                _load_from_dict(_local_data)
                logger.info("✅ تم تحميل البيانات من bot_data.json (وضع احتياطي).")
            except Exception as _e:
                logger.warning(f"⚠️ فشل تحميل bot_data.json: {_e}")
        return
    try:
        data = col.find_one({"_id": "bot_data"})
        if not data:
            logger.info("ℹ️ لا توجد بيانات محفوظة في MongoDB بعد.")
            return
        _load_from_dict(data)
        logger.info("✅ تم تحميل البيانات من MongoDB بنجاح.")
    except Exception as e:
        logger.warning(f"⚠️ فشل تحميل البيانات من MongoDB: {e}")


# ─── حفظ/حذف السشنات النشطة في MongoDB ───────────────────────────────────────

def _db_save_session(chat_id: int, sess_id: int):
    """يحفظ سشناً واحداً في MongoDB."""
    col = _get_sessions_col()
    if col is None:
        return
    session = (_sessions.get(chat_id) or {}).get(sess_id)
    if not session:
        return
    try:
        participants_data = []
        for p in session.get("participants", []):
            pdata = dict(p)
            if isinstance(pdata.get("joined_at"), datetime):
                pdata["joined_at"] = pdata["joined_at"].isoformat()
            participants_data.append(pdata)
        started_at = session.get("started_at")
        if isinstance(started_at, datetime):
            started_at = started_at.isoformat()
        doc = {
            "_id": f"{chat_id}:{sess_id}",
            "chat_id": chat_id,
            "sess_id": sess_id,
            "study": session["study"],
            "break": session["break"],
            "participants": participants_data,
            "creator_name": session.get("creator_name", ""),
            "creator_id": session.get("creator_id"),
            "message_id": session.get("message_id"),
            "session_num": session.get("session_num", 1),
            "phase": session.get("phase", "waiting"),
            "started_at": started_at,
        }
        col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    except Exception as e:
        logger.warning(f"⚠️ فشل حفظ السشن في MongoDB: {e}")


def _db_delete_session(chat_id: int, sess_id: int):
    """يحذف سشناً واحداً من MongoDB."""
    col = _get_sessions_col()
    if col is None:
        return
    try:
        col.delete_one({"_id": f"{chat_id}:{sess_id}"})
    except Exception as e:
        logger.warning(f"⚠️ فشل حذف السشن من MongoDB: {e}")


def _db_clear_group_sessions(chat_id: int):
    """يحذف جميع سشنات مجموعة من MongoDB."""
    col = _get_sessions_col()
    if col is None:
        return
    try:
        col.delete_many({"chat_id": chat_id})
    except Exception as e:
        logger.warning(f"⚠️ فشل مسح سشنات المجموعة من MongoDB: {e}")


async def _restore_sessions_from_db(bot):
    """يستعيد السشنات النشطة من MongoDB عند إعادة تشغيل البوت."""
    col = _get_sessions_col()
    if col is None:
        return
    try:
        docs = list(col.find({}))
        if not docs:
            return
        restored = 0
        for doc in docs:
            chat_id = doc["chat_id"]
            sess_id = doc["sess_id"]
            # إعادة بناء المشاركين
            participants = []
            for p in doc.get("participants", []):
                pdata = dict(p)
                if pdata.get("joined_at"):
                    try:
                        pdata["joined_at"] = datetime.fromisoformat(pdata["joined_at"])
                    except Exception:
                        pdata["joined_at"] = None
                participants.append(pdata)
            # تحويل started_at
            started_at = None
            if doc.get("started_at"):
                try:
                    started_at = datetime.fromisoformat(doc["started_at"])
                except Exception:
                    pass
            # بناء السشن في الذاكرة
            if chat_id not in _sessions:
                _sessions[chat_id] = {}
            _sessions[chat_id][sess_id] = {
                "study": doc["study"],
                "break": doc["break"],
                "participants": participants,
                "creator_name": doc.get("creator_name", ""),
                "creator_id": doc.get("creator_id"),
                "message_id": doc.get("message_id"),
                "session_num": doc.get("session_num", 1),
                "phase": doc.get("phase", "waiting"),
                "started_at": started_at,
                "task": None,
                "sess_id": sess_id,
            }
            if sess_id > _session_counters.get(chat_id, 0):
                _session_counters[chat_id] = sess_id
            # إذا كان السشن في مرحلة الدراسة، أعد تشغيل المؤقت بالوقت المتبقي
            if doc.get("phase") == "studying" and started_at:
                elapsed = int((datetime.now() - started_at).total_seconds())
                task = asyncio.create_task(
                    run_session_timer(chat_id, sess_id, bot, elapsed_seconds=elapsed)
                )
                _sessions[chat_id][sess_id]["task"] = task
            restored += 1
        logger.info(f"✅ تم استعادة {restored} سشن نشط من MongoDB.")
    except Exception as e:
        logger.warning(f"⚠️ فشل استعادة السشنات من MongoDB: {e}")


# {user_id} — المستخدمين اللي استخدموا فرصة المسامحة مرة واحدة
_forgiven_users: set = set()

FORGIVENESS_PHRASES = [
    "سامحيني", "سامحيني اميرة", "آسف", "اسف", "معذرة", "معلش",
    "آسف اميرة", "اسف اميرة", "عذرا", "عذراً", "اعتذر",
    "مو قصدي", "مو قصدي اميرة", "ما قصدت",
    "اخطأت", "غلطت",
]


def is_asking_forgiveness(text: str) -> bool:
    text_lower = text.strip().lower()
    for phrase in FORGIVENESS_PHRASES:
        if phrase in text_lower:
            return True
    return False

# ============================================================
# 🔞 قائمة الشتائم — مع تطبيع النص لتجنب الأخطاء
# ============================================================
PROFANITY_WORDS = [
    # جنسي صريح
    "كس", "كوس", "طيز", "زب", "عير", "اير", "نيك", "نييك", "ينيك",
    "منيوك", "منيوكه", "مفعول", "مص",
    # شتائم موجهة لأشخاص
    "شرموطه", "شرموط", "عاهره", "قحبه", "خول", "لوطي", "شاذ",
    "ابن زنا", "ابن حرام", "ولد حرام", "بنت حرام", "ابن متناكه",
    # حيوانات كإهانة (كلمة منفردة)
    "كلب", "حمار", "خنزير", "قرد", "حيوان",
    # إنجليزي
    "fuck", "shit", "bitch", "asshole", "pussy", "cock", "dick", "whore",
]


def normalize_arabic(text: str) -> str:
    import re
    # حذف التشكيل
    text = re.sub(r'[\u064B-\u065F\u0670]', '', text)
    # توحيد أشكال الألف
    text = re.sub(r'[أإآ]', 'ا', text)
    # توحيد الياء والألف المقصورة
    text = re.sub(r'ى', 'ي', text)
    # توحيد التاء المربوطة والهاء
    text = re.sub(r'ة', 'ه', text)
    # حذف التطويل
    text = re.sub(r'ـ', '', text)
    # حذف الحروف المكررة أكثر من مرتين (مثل كلببب)
    text = re.sub(r'(.)\1{2,}', r'\1\1', text)
    return text.lower()


def contains_profanity(text: str) -> bool:
    import re
    normalized = normalize_arabic(text)
    for word in PROFANITY_WORDS:
        norm_word = normalize_arabic(word)
        # تطابق كلمة كاملة (word boundary)
        pattern = r'(?<![ا-ي\w])' + re.escape(norm_word) + r'(?![ا-ي\w])'
        if re.search(pattern, normalized):
            return True
    return False


# ============================================================
# 📢 الكلمات اللي تنادي البوت (اسم البوت وما يشبهه)
# تقدر تضيف أسماء ثانية أو تحذف منها
# ============================================================
BOT_TRIGGER_WORDS = [
    "اميرة", "أميرة", "بوت", "bot",
    "يا اميرة", "يا أميرة", "امورة",
    "اميرهه", "أميرهه", "بووت", "اموره",
]



def detect_session_request(text: str) -> bool:
    """يكتشف إذا كان الشخص يريد بدء سشن دراسة.
    الأمر لازم يكون الرسالة كلها بالضبط — وليس وسط جملة أو في بداية رسالة أطول."""
    t = text.strip()
    tl = t.lower()
    SESSION_TRIGGERS = [
        "سشن", "session", "pomodoro", "بومودورو",
        "جلسة دراسة", "سشن دراسة", "ابدأ سشن", "ابدي سشن",
        "بدء سشن", "سوي سشن", "اعملي سشن", "اعمل سشن",
        "ابدأي سشن", "بدي سشن",
    ]
    for trigger in SESSION_TRIGGERS:
        tr = trigger.lower()
        # الرسالة كلها = الأمر فقط — تطابق تام
        if tl == tr:
            return True
    return False


# ============================================================
# 💬 ردود البوت لما ينادونه
# تقدر تعدل الردود أو تضيف ردود جديدة
# ============================================================
BOT_RESPONSES = [
    "نعم، تفضل.",
    "أيوه، أمر.",
    "نعم، شبيك؟",
    "تفضل، أسمعك.",
    "هلا، شبيك؟",
    "أيوه؟",
    "نعم، شتريد؟",
    "تفضل، أنا هنا.",
    "هلا فيك، شبيك؟",
    "نعم.",
    "أسمعك، تفضل.",
    "هلا.",
    "نعم؟",
    "شبيك؟",
    "مالي خلك.",
    "شتريد؟",
]

_GREET_RESPONSES = [
    "بخير ولله، وأنت؟",
    "الحمدلله، شلونك أنت؟",
    "بألف خير، وأنت شلونك؟",
    "زين ولله، وأنت بخير إن شاء الله؟",
    "زين، وأنت شلونك؟",
    "الحمدلله بخير، وأنت؟",
    "بخير، شلونك أنت؟",
    "ولله زين، وأنت؟",
    "زين وبصحة، شلونك؟",
    "الحمدلله، وأنت بألف خير إن شاء الله.",
]

_SALAM_RESPONSES = [
    "وعليكم السلام ورحمة الله وبركاته، هلا وغلا!",
    "وعليكم السلام، هلا بيك!",
    "وعليكم السلام، أهلاً وسهلاً!",
    "وعليكم السلام ورحمة الله،",
    "وعليكم السلام، تفضل.",
    "وعليكم السلام.",
]

_MORNING_RESPONSES = [
    "صباح النور، هلا وغلا!",
    "صباح الورد، شلونك؟",
    "صباح الخير والبركة، هلا بيك!",
    "صباح النور عليك، كيف الحال؟",
    "وعليك الصباح بالنور والخير!",
    "صباح الخير، الله يصبحك بخير!",
    "صباح النور، الله يصبحك بالخير والعافية.",
    "وعليك صباح الخير، شلونك اليوم؟",
]

_EVENING_RESPONSES = [
    "مساء النور، هلا وغلا!",
    "مساء الخير والبركة، كيف الحال؟",
    "وعليك مساء النور!",
    "مساء الورد، شلونك؟",
    "الله يمسيك بالخير، هلا بيك!",
    "مساء الخير، الله يمسيك بخير.",
]

_LOVE_RESPONSES = [
    "هلا، ربي يخليك، شبيك؟",
    "هلا بيك، الله يحفظك، أمرني.",
    "ربي يخليك، تفضل.",
    "والله أنت تعبت روحي، شبيك؟",
    "هلا فيك، ربي يحفظك ويخليك.",
    "يسلمك، شبيك؟",
    "الله يخليك، تفضل أسمعك.",
    "هلا، أنت عزيز، شتريد؟",
    "ربي يحفظك، شبيك؟",
]

_THANKS_RESPONSES = [
    "العفو، بخدمتك.",
    "لا شكر على واجب.",
    "هلا فيك، بخدمتك.",
    "العفو.",
    "لا تشكر، هذا اللي أقدر أسويه.",
    "يسلمك، بخدمتك.",
    "لا شكر على واجب، أمرني بأي شيء ثاني.",
    "بخير خاطرك، لا تتردد.",
    "هلا، بخدمتك.",
]

_COMPLIMENT_RESPONSES = [
    "شكراً، يسلمك.",
    "الله يسلمك، تفضل.",
    "هذا من ذوقك.",
    "يسلم فمك، شبيك؟",
    "شكراً، تفضل أمرني.",
    "ربي يخليك، شبيك؟",
    "من ذوقك هذا الكلام.",
    "يسلمك.",
]

_FAREWELL_RESPONSES = [
    "مع السلامة، ربي يحفظك.",
    "الله يسلمك، خش بسلامة.",
    "تصبح على خير.",
    "مع السلامة.",
    "يسلمك، ربي يوفقك.",
    "الله يحفظك.",
    "تصبح على خير، ربي يسلمك.",
    "مع السلامة.",
]

_BORED_RESPONSES = [
    "روح سوي شيء نافع بدل الفراغ.",
    "الفراغ مو زين، دور على شيء تسويه.",
    "لو ملل اقرأ شيء أو ادرس.",
    "الملل علامة الفراغ، فكر بشيء مفيد.",
    "روح تمشى أو اقرأ، الجلوس ما يفيد.",
]

_ANGRY_RESPONSES = [
    "لا تضيع طاقتك بالزعل.",
    "تنفس وهدّ، الدنيا ما تستاهل الزعل.",
    "اهدى شوي، وقلي شبيك.",
    "الزعل ما يحل شيء.",
    "تنفس وقلي شبيك.",
]

_QUESTION_GENERAL_RESPONSES = [
    "سؤالك وصلني، بس هالحين ما أقدر أجاوب، جرب بعد شوي.",
    "ماعندي جواب هالحين، جرب بعد شوي.",
    "لو تعيد السؤال بعد شوي أجاوبك إن شاء الله.",
    "سؤال زين، بس هالحين ما أقدر أكمّل، جرب ثانية.",
]

_HELP_RESPONSES = [
    "بخير خاطرك، شتريد أساعدك فيه؟",
    "هلا، قلي شبيك وأساعدك.",
    "تفضل، أنا هنا، شبيك؟",
    "شرف أساعدك، قلي شبيك.",
    "أسمعك، شتريد تسوي؟",
    "قلي المشكلة وأشوف أقدر أساعد.",
    "موجود، قلي شبيك.",
]

_FOOD_RESPONSES = [
    "والله الأكل موضوع مهم، بس ما أقدر أساعدك فيه هالحين.",
    "جوعان؟ روح اتغدى وارجع.",
    "الأكل روح دور، أنا ماأطبخ.",
    "أفكر... لا ما أعرف أطبخ.",
    "الأكل الحلو يحتاج طباخ، أنا بس بوت.",
    "روح طبخ شيء وارجع، هنا ما أقدر أساعدك بالأكل.",
    "جوعان؟ الثلاجة هي صاحبك مو أنا.",
]

_STUDY_HELP_RESPONSES = [
    "الدراسة شيء مهم، حاول تبحث بشكل أدق وأساعدك.",
    "ارسل السؤال كامل وأشوف أقدر أساعدك.",
    "الدراسة تحتاج تركيز، جرب ترسل السؤال بتفصيل.",
    "لو عندك سؤال دراسي ارسله وأحاول أساعدك.",
    "الامتحانات صعبة، بس ارسل سؤالك وأشوف.",
    "الدرس هذا يحتاج تفاصيل أكثر، ارسل السؤال كامل.",
]

_SAD_RESPONSES = [
    "والله ما يهون، بس الدنيا تتغير.",
    "كل شيء يعدي، ثق بالله.",
    "الحزن مو نهاية، باجر أحسن إن شاء الله.",
    "أنا هنا أسمعك، قلي شبيك.",
    "لا تنكسر، كل عقبة تعدي.",
    "ربي يفرجها عليك، لا تيأس.",
    "الضيقة تعدي، اصبر.",
    "كل شيء صعب إله نهاية، ثق.",
]

_HAPPY_RESPONSES = [
    "الله يديم فرحتك!",
    "زين، الله يكملها بالخير.",
    "هلا بالفرح، الله يديمه عليك.",
    "ماشاءالله، ربي يزيدك.",
    "هذا اللي نبيه، الله يبارك.",
    "الفرح يعدي عليك دايم إن شاء الله.",
    "والله يسعدني أسمع هذا، الله يكملها.",
]

_HEALTH_RESPONSES = [
    "الله يشفيك، راجع الدكتور بس.",
    "سلامتك، ربي يعافيك.",
    "إن شاء الله تعافى سريع.",
    "الصحة أهم شيء، روح الدكتور لو الوجع شديد.",
    "ربي يسلمك ويعافيك.",
    "سلامتك، استرح وروح الدكتور.",
    "الله يحفظك ويعافيك.",
]

_JOKE_RESPONSES = [
    "أنا بوت، النكت مو تخصصي.",
    "نكتة؟ والله ماعندي بس الموقف كله نكتة.",
    "الضحك على الفاضي مو شغلتي.",
    "طلبت نكتة من بوت؟ هذا هو الظرف.",
    "هاك نكتة — أنا بوت وتسألني نكت.",
    "النكت اللي عندي ما تضحك، ثق.",
]

_WEATHER_RESPONSES = [
    "الطقس ماأعرفه، بس العراق دايم حار.",
    "افتح تطبيق الطقس، ماعندي أخبار الجو.",
    "الجو هالفترة بين بين، بس ما أضمن لك شيء.",
    "روح اشوف من الشباك أحسن.",
    "الطقس اليوم؟ والله ما أعرف، جرب تطبيق الطقس.",
]

_TIME_RESPONSES = [
    "الوقت؟ شوف الساعة على موبايلك أسرع مني.",
    "أنا بوت، الساعة على شاشتك مو عندي.",
    "شوف الموبايل، أسرع.",
    "الساعة مو من اختصاصي، موبايلك يعرف.",
]

_OPINION_RESPONSES = [
    "رأيي مو مهم، أنت اللي تقرر.",
    "والله ما أعرف، أنت أعرف بحالك.",
    "قرر أنت، أنا بوت.",
    "الرأي يرجع لك أنت.",
    "كل واحد وذوقه.",
    "ما أقدر أقرر عنك، قرر أنت.",
]

_RELIGION_RESPONSES = [
    "الله يبارك فيك.",
    "ربي يحفظك ويسلمك.",
    "إن شاء الله، ربي يوفقك.",
    "الله يرزقك ويعافيك.",
    "ربي يكون بعونك.",
    "دعواتك مستجابة إن شاء الله.",
    "الله يحفظك من كل شر.",
]

_NOSLEEP_RESPONSES = [
    "السهر مو زين، روح نام.",
    "ليش ما تنام؟ الجسم يحتاج راحة.",
    "السهر يتعب الجسم، استرح شوي.",
    "النوم مهم، لا تسهر زيادة.",
    "روح نام وارجع باجر بذهن صافي.",
]

_MONEY_RESPONSES = [
    "الفلوس موضوع حساس، ما أقدر أساعدك فيه بدون تفاصيل.",
    "الرزق بيد الله، بس ابذل جهدك.",
    "الفلوس تجي وتروح، المهم صحتك.",
    "مو متخصص بالفلوس، بس ربي يرزقك.",
    "الله يرزقك ويفتح عليك.",
]

_RELATIONSHIP_RESPONSES = [
    "الموضوع هذا حساس، أنا بوت ما أقدر أحكم.",
    "العلاقات صعبة، بس التفاهم أساسها.",
    "قرر بهدوء، القرارات المتسرعة ما تنفع.",
    "كل موقف وإله وضعه، فكر زين.",
    "الموضوع يرجع لك أنت، أنا ما أقدر أقرر.",
]

_POLITICS_RESPONSES = [
    "السياسة موضوع ما أدخل فيه.",
    "السياسة باب مسدود عندي.",
    "هذا الموضوع ما أعلق عليه.",
    "كل واحد ورأيه بالسياسة، أنا بعيد.",
]

_TECH_RESPONSES = [
    "التقنية موضوعي، بس هالحين ما أقدر أساعد.",
    "سؤال تقني؟ ارسله بتفصيل وأشوف.",
    "الموبايل والبرامج مو مشكلة، بس اشرح أكثر.",
    "أسئلة التقنية أحب أجاوبها، بس هالحين ما أقدر أكمّل.",
]

_MUSIC_RESPONSES = [
    "تريد أغنية؟ قلي «شغل» واسم الأغنية وأشغلها لك.",
    "تبي أغنية؟ قلي «شغل» واسمها.",
    "الأغاني عندي عبر اليوتيوب، قلي اسم الأغنية.",
    "قلي «شغل» واسم الأغنية وأجيبها لك من اليوتيوب.",
]

_GAME_RESPONSES = [
    "الألعاب مو تخصصي، بس اسأل وأشوف.",
    "ألعاب؟ اسأل وأحاول أساعد.",
    "الألعاب موضوع كبير، شتبي تعرف؟",
    "أنا بوت مو لاعب، بس اسأل.",
]

_TRAVEL_RESPONSES = [
    "السفر حلو، وين تبي تروح؟",
    "السفر تجربة، بس ما أقدر أنظم لك الرحلة.",
    "وين تبي تسافر؟ فكرة حلوة.",
    "السفر هواية جميلة، الله يوفقك.",
]

_AFRAID_RESPONSES = [
    "لا تخاف، كل شيء بيد الله.",
    "الخوف طبيعي، بس ثق بالله.",
    "لا تخاف، ربي معك.",
    "الخوف ما يحل المشكلة، فكر بهدوء.",
]

_MISS_RESPONSES = [
    "الله يجمعكم على خير.",
    "الشوق شيء حلو، إن شاء الله تشوفه قريب.",
    "ربي يجمعكم بخير.",
    "الشوق يعني الحب، إن شاء الله يزول.",
]

_RANDOM_WISDOM = [
    "اللي ما يتعلم من الصغر يتعلم من الكبر — بس بثمن أغلى.",
    "الصبر مفتاح الفرج.",
    "اللي بالقلب يظهر على اللسان.",
    "كل يوم بيومه، لا تتعب روحك بالتفكير.",
    "الوقت ما يرجع، استثمره.",
    "البساطة أحياناً أحسن حل.",
    "الهدوء قوة مو ضعف.",
]

_COMPLAINT_BOT_RESPONSES = [
    "حسناً، ما راح أزيد.",
    "فهمت، بكمّل بهدوء.",
    "زين، ما راح أطول.",
    "واضح، راح أخففها.",
    "حسناً، فهمت.",
    "اوكي، ما راح أكمل.",
    "فاهم، ما راح أطول عليك.",
]

_UNCLEAR_RESPONSES = [
    "ما فهمت قصدك، وضح أكثر.",
    "مو واضح علي، قلي أكثر.",
    "شو تقصد بالضبط؟",
    "وضح شوي، ما فهمت.",
    "قلي أكثر، ما اتضح الموضوع.",
    "مو فاهم، اشرح أكثر.",
    "ما فهمت، تقدر تعيد؟",
]


def get_smart_fallback(first_name: str, message: str) -> str:
    msg = message.strip().lower()

    # الكشف يعتمد على وجود العبارة كاملة في النص (متلاصقة) بغض النظر عن طول الرسالة
    salam_words = ["السلام عليكم", "سلام عليكم", "السلام عليكم ورحمة الله", "السلام عليكم ورحمة الله وبركاته"]
    morning_words = ["صباح الخير", "صباح الياسمين", "صباح العسل"]
    evening_words = ["مساء الخير", "مساء الورد"]
    greet_words = ["شلونك", "كيفك", "كيف حالك", "كيف الحال", "شخبارك", "شلون حالك", "عامل ايش", "عامل إيش", "شو أخبارك", "شو اخبارك"]

    if any(w in msg for w in salam_words):
        return random.choice(_SALAM_RESPONSES)
    if any(w in msg for w in morning_words):
        return random.choice(_MORNING_RESPONSES)
    if any(w in msg for w in evening_words):
        return random.choice(_EVENING_RESPONSES)
    if any(w in msg for w in greet_words):
        return random.choice(_GREET_RESPONSES)

    return None




# ============================================================
# 🗂️ بيانات مؤقتة — لا تعدل هنا
# ============================================================
warn_data = {}
profanity_violations = {}


# ============================================================
# أوامر الإدارة العربية — لا تعدل هنا
# ============================================================
ARABIC_COMMANDS = {
    "حظر": "ban",
    "الغاء الحظر": "unban",
    "رفع الحظر": "unban",
    "كتم": "mute",
    "رفع كتم": "unmute",
    "الغاء الكتم": "unmute",
    "طرد": "kick",
    "انذار": "warn",
    "تثبيت": "pin",
    "إلغاء تثبيت": "unpin",
    "الغاء التثبيت": "unpin",
    "حذف رد": "delete_reply",
    "حذف": "delete",
    "معلومات": "info",
    "كشف": "info",
    "حد الرسائل": "rate_limit",
    "حد رسائل": "rate_limit",
    "الغاء حد الرسائل": "cancel_rate_limit",
    "إلغاء حد الرسائل": "cancel_rate_limit",
    "رفع حد الرسائل": "cancel_rate_limit",
    "وقف حد الرسائل": "cancel_rate_limit",
    "رفع مشرف": "promote",
    "تنزيل عضو": "demote",
    "اضافة رد": "add_reply",
    "إضافة رد": "add_reply",
    "قائمة الردود": "list_replies",
    "انهاء سشن": "end_session",
    "إنهاء سشن": "end_session",
    "اغلاق سشن": "end_session",
    "إغلاق سشن": "end_session",
    "ايقاف السشن": "end_session",
    "إيقاف السشن": "end_session",
    "الغاء السشن": "end_session",
    "إلغاء السشن": "end_session",
    "وقف السشن": "end_session",
    "الانسحاب": "leave_session",
    "انسحاب": "leave_session",
    "الانسحاب من السشن": "leave_session",
    "انسحاب من السشن": "leave_session",
    "السشنات": "active_sessions",
    "السشنات النشطة": "active_sessions",
    "السشنات النشطه": "active_sessions",
    "الغاء منع التسخيت": "stop_focus",
    "إلغاء منع التسخيت": "stop_focus",
    "ايقاف منع التسخيت": "stop_focus",
    "إيقاف منع التسخيت": "stop_focus",
    "وقف منع التسخيت": "stop_focus",
    "مساعدة": "help",
    "الاوامر": "help",
    "الأوامر": "help",
    "اوامر": "help",
    "أوامر": "help",
    "الاحصائيات": "stats",
    "الإحصائيات": "stats",
    "احصائيات": "stats",
    "إحصائيات": "stats",
    "احصائياتي": "my_stats",
    "إحصائياتي": "my_stats",
    # الترحيب
    "تفعيل الترحيب": "enable_welcome",
    "تشغيل الترحيب": "enable_welcome",
    "تعطيل الترحيب": "disable_welcome",
    "إيقاف الترحيب": "disable_welcome",
    "وقف الترحيب": "disable_welcome",
    # تقييد الوسائط — الصور
    "تعطيل الصور": "restrict_photo",
    "قفل الصور": "restrict_photo",
    "منع الصور": "restrict_photo",
    "وقف الصور": "restrict_photo",
    "إيقاف الصور": "restrict_photo",
    "ايقاف الصور": "restrict_photo",
    "تفعيل الصور": "allow_photo",
    "فتح الصور": "allow_photo",
    "السماح بالصور": "allow_photo",
    "سماح الصور": "allow_photo",
    "تشغيل الصور": "allow_photo",
    # الفيديو
    "تعطيل الفيديو": "restrict_video",
    "قفل الفيديو": "restrict_video",
    "منع الفيديو": "restrict_video",
    "وقف الفيديو": "restrict_video",
    "إيقاف الفيديو": "restrict_video",
    "ايقاف الفيديو": "restrict_video",
    "تفعيل الفيديو": "allow_video",
    "فتح الفيديو": "allow_video",
    "السماح بالفيديو": "allow_video",
    "سماح الفيديو": "allow_video",
    "تشغيل الفيديو": "allow_video",
    # الملفات
    "تعطيل الملفات": "restrict_document",
    "قفل الملفات": "restrict_document",
    "منع الملفات": "restrict_document",
    "وقف الملفات": "restrict_document",
    "إيقاف الملفات": "restrict_document",
    "ايقاف الملفات": "restrict_document",
    "تفعيل الملفات": "allow_document",
    "فتح الملفات": "allow_document",
    "السماح بالملفات": "allow_document",
    "سماح الملفات": "allow_document",
    "تشغيل الملفات": "allow_document",
    # الستيكر
    "تعطيل الستيكر": "restrict_sticker",
    "قفل الستيكر": "restrict_sticker",
    "منع الستيكر": "restrict_sticker",
    "وقف الستيكر": "restrict_sticker",
    "إيقاف الستيكر": "restrict_sticker",
    "ايقاف الستيكر": "restrict_sticker",
    "تعطيل الستيكرات": "restrict_sticker",
    "قفل الستيكرات": "restrict_sticker",
    "منع الستيكرات": "restrict_sticker",
    "تفعيل الستيكر": "allow_sticker",
    "فتح الستيكر": "allow_sticker",
    "السماح بالستيكر": "allow_sticker",
    "سماح الستيكر": "allow_sticker",
    "تشغيل الستيكر": "allow_sticker",
    "تفعيل الستيكرات": "allow_sticker",
    "فتح الستيكرات": "allow_sticker",
    # الصوت
    "تعطيل الصوت": "restrict_voice",
    "قفل الصوت": "restrict_voice",
    "منع الصوت": "restrict_voice",
    "وقف الصوت": "restrict_voice",
    "إيقاف الصوت": "restrict_voice",
    "ايقاف الصوت": "restrict_voice",
    "تعطيل الرسائل الصوتية": "restrict_voice",
    "قفل الرسائل الصوتية": "restrict_voice",
    "منع الرسائل الصوتية": "restrict_voice",
    "تفعيل الصوت": "allow_voice",
    "فتح الصوت": "allow_voice",
    "السماح بالصوت": "allow_voice",
    "سماح الصوت": "allow_voice",
    "تشغيل الصوت": "allow_voice",
    "تفعيل الرسائل الصوتية": "allow_voice",
    "فتح الرسائل الصوتية": "allow_voice",
    # الموسيقى
    "تعطيل الموسيقى": "restrict_audio",
    "قفل الموسيقى": "restrict_audio",
    "منع الموسيقى": "restrict_audio",
    "وقف الموسيقى": "restrict_audio",
    "إيقاف الموسيقى": "restrict_audio",
    "ايقاف الموسيقى": "restrict_audio",
    "تفعيل الموسيقى": "allow_audio",
    "فتح الموسيقى": "allow_audio",
    "السماح بالموسيقى": "allow_audio",
    "سماح الموسيقى": "allow_audio",
    "تشغيل الموسيقى": "allow_audio",
    # الجيف
    "تعطيل الجيف": "restrict_animation",
    "قفل الجيف": "restrict_animation",
    "منع الجيف": "restrict_animation",
    "وقف الجيف": "restrict_animation",
    "إيقاف الجيف": "restrict_animation",
    "ايقاف الجيف": "restrict_animation",
    "تعطيل الصور المتحركة": "restrict_animation",
    "قفل الصور المتحركة": "restrict_animation",
    "منع الصور المتحركة": "restrict_animation",
    "تفعيل الجيف": "allow_animation",
    "فتح الجيف": "allow_animation",
    "السماح بالجيف": "allow_animation",
    "سماح الجيف": "allow_animation",
    "تشغيل الجيف": "allow_animation",
    "تفعيل الصور المتحركة": "allow_animation",
    "فتح الصور المتحركة": "allow_animation",
    # المميزون
    "رفع مميز": "vip_add",
    "تنزيل مميز": "vip_remove",
    "قائمة المميزين": "vip_list",
    # تقييد/رفع الكل
    "تعطيل الكل": "restrict_all",
    "قفل الكل": "restrict_all",
    "منع الكل": "restrict_all",
    "وقف الكل": "restrict_all",
    "قفل كل شيء": "restrict_all",
    "تفعيل الكل": "allow_all",
    "فتح الكل": "allow_all",
    "السماح بالكل": "allow_all",
    "فتح كل شيء": "allow_all",
}


async def get_target_user(update: Update):
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


async def get_target_user_extended(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يحصل على المستخدم المستهدف عبر الرد أو text_mention أو @يوزرنيم مخزّن بالكاش."""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    # 1) فحص كيانات الرسالة — text_mention يحتوي User مباشرة
    for entity in (update.message.entities or []):
        if entity.type == "text_mention" and entity.user:
            return entity.user

    # 2) فحص كيانات mention (@username) والبحث في الكاش
    msg_text = update.message.text or ""
    for entity in (update.message.entities or []):
        if entity.type == "mention":
            raw = msg_text[entity.offset: entity.offset + entity.length]  # مثال: @sn_sr7
            uname = raw.lstrip("@").lower()
            uid = _username_to_id.get(uname)
            if uid:
                user_obj = _id_to_user.get(uid)
                if user_obj:
                    return user_obj
                try:
                    member = await context.bot.get_chat_member(update.effective_chat.id, uid)
                    return member.user
                except Exception:
                    pass

    # 3) fallback: بحث regex في النص + الكاش
    import re
    match = re.search(r'@(\w+)', msg_text)
    if match:
        uname = match.group(1).lower()
        uid = _username_to_id.get(uname)
        if uid:
            user_obj = _id_to_user.get(uid)
            if user_obj:
                return user_obj
            try:
                member = await context.bot.get_chat_member(update.effective_chat.id, uid)
                return member.user
            except Exception:
                pass

    return None


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == OWNER_CHAT_ID:
        return True
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except TelegramError:
        return False


async def is_admin_by_id(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except TelegramError:
        return False


async def _is_group_creator(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """يتحقق إذا المستخدم هو مالك (creator) هذه المجموعة تحديداً."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status == "creator"
    except Exception:
        return False


def _is_whole_word(keyword: str, text: str) -> bool:
    """يتحقق إذا الكلمة موجودة كلمة مستقلة وليس ضمن كلمة أخرى."""
    words = re.split(r'[\s،.؟?!,،؛:()\[\]{}"\'«»\-_\u200c\u200d]+', text)
    return keyword in words


def arabic_error(e: Exception) -> str:
    """يترجم خطأ Telegram إلى رسالة عربية واضحة."""
    msg = str(e).lower()
    if "not enough rights" in msg or "admin privileges" in msg or ("administrator" in msg and "required" in msg):
        return "البوت لا يملك الصلاحيات الكافية"
    if "can't restrict self" in msg or "can't demote self" in msg or "can't promote self" in msg:
        return "لا يمكن تطبيق الأمر على البوت نفسه"
    if "can't demote chat creator" in msg:
        return "لا يمكن تنزيل مالك المجموعة"
    if "user is an administrator" in msg:
        return "المستخدم مشرف بالفعل"
    if "user not found" in msg or "peer_id_invalid" in msg or "participant_id_invalid" in msg:
        return "المستخدم غير موجود"
    if "user is not a member" in msg or "not a member" in msg or "user_not_participant" in msg:
        return "المستخدم ليس في المجموعة"
    if "message to delete not found" in msg or "message_id_invalid" in msg or "message not found" in msg:
        return "الرسالة غير موجودة أو تم حذفها"
    if "flood" in msg or "too many requests" in msg or "retry after" in msg:
        return "كثرة الطلبات، انتظر قليلاً ثم حاول"
    if "forbidden" in msg or "bot was blocked" in msg or "bot is not a member" in msg:
        return "ليس للبوت صلاحية تنفيذ هذا الإجراء"
    if "rights not modified" in msg:
        return "الصلاحيات لم تتغير"
    if "chat not found" in msg or "chat_id_invalid" in msg:
        return "المجموعة غير موجودة"
    if "message is not modified" in msg:
        return "لم يطرأ أي تغيير على الرسالة"
    return "تعذّر تنفيذ الأمر — تأكد من صلاحيات البوت وحاول مجدداً"




def is_calling_bot(text: str) -> bool:
    text_stripped = text.strip().lower()
    for trigger in BOT_TRIGGER_WORDS:
        t = trigger.lower()
        if text_stripped == t or text_stripped.startswith(t + " ") or text_stripped.startswith(t + "\n"):
            return True
    return False


async def mute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    await context.bot.restrict_chat_member(chat_id, user_id, permissions)


async def profanity_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not update.effective_chat or update.effective_chat.type == "private":
        return

    user = update.effective_user
    if user.id == OWNER_CHAT_ID:
        return
    chat_id = update.effective_chat.id
    text = update.message.text

    if await is_admin_by_id(context, chat_id, user.id):
        return

    if not contains_profanity(text):
        return

    user_name = user.full_name
    user_id = user.id
    username_tag = f"@{user.username}" if user.username else f"[{user_name}](tg://user?id={user_id})"
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        await update.message.delete()
    except TelegramError as e:
        logger.warning(f"Could not delete message: {e}")
        return

    key = f"{chat_id}_{user_id}"
    profanity_violations[key] = profanity_violations.get(key, 0) + 1
    count = profanity_violations[key]

    logger.info(f"Profanity from {user_id} ({user_name}) in {chat_id} — violation #{count}")

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_tags = []
        for admin in admins:
            if not admin.user.is_bot:
                if admin.user.username:
                    admin_tags.append(f"@{admin.user.username}")
                else:
                    admin_tags.append(f"[{admin.user.full_name}](tg://user?id={admin.user.id})")
        admins_mention = " ".join(admin_tags)
    except TelegramError:
        admins_mention = "المشرفين"

    if count >= 3:
        try:
            await mute_user(context, chat_id, user_id)
            profanity_violations[key] = 0
            alert_text = (
                f"🚨 *تنبيه للمشرفين* 🚨\n\n"
                f"تم *كتم* المستخدم {username_tag} تلقائياً بسبب تكرار المخالفات \\(3 مرات\\)\\.\n\n"
                f"🕐 الوقت: `{time_str}`\n\n"
                f"⚠️ {admins_mention}\n"
                f"يرجى مراجعة الموقف واتخاذ الإجراء المناسب\\."
            )
        except TelegramError as e:
            alert_text = (
                f"🚨 *تنبيه للمشرفين* 🚨\n\n"
                f"المستخدم {username_tag} كرر المخالفات 3 مرات لكن فشل الكتم\\.\n\n"
                f"🕐 `{time_str}`\n\n"
                f"⚠️ {admins_mention}"
            )
    else:
        remaining = 3 - count
        warning_line = "⚠️ تحذير: مخالفة أخرى وسيتم الكتم التلقائي\\!" if remaining == 1 else f"متبقي: {remaining} مخالفات للكتم التلقائي"
        alert_text = (
            f"🛡 *تم حذف رسالة مخالفة*\n\n"
            f"المستخدم: {username_tag}\n"
            f"⚠️ عدد المخالفات: *{count}/3*\n"
            f"{warning_line}\n\n"
            f"🕐 `{time_str}`\n\n"
            f"👮 {admins_mention}\n"
            f"إذا رأيتم أن المخالفة متعمدة يمكنكم استخدام أمر: حظر"
        )

    try:
        await context.bot.send_message(chat_id=chat_id, text=alert_text, parse_mode="MarkdownV2")
    except TelegramError as e:
        logger.error(f"Failed to send profanity alert: {e}")


def _help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔨 أوامر الإدارة", callback_data="help_admin"),
         InlineKeyboardButton("👑 أوامر المالك", callback_data="help_owner")],
        [InlineKeyboardButton("🎯 جلسات الدراسة", callback_data="help_study"),
         InlineKeyboardButton("🚫 منع التسخيت", callback_data="help_focus")],
        [InlineKeyboardButton("💬 الردود التلقائية", callback_data="help_replies"),
         InlineKeyboardButton("🎬 الفيديو والصوت", callback_data="help_media")],
        [InlineKeyboardButton("🤖 التحدث مع أميرة", callback_data="help_bot"),
         InlineKeyboardButton("🛡 فلتر الشتائم", callback_data="help_filter")],
        [InlineKeyboardButton("📵 تقييد الوسائط", callback_data="help_restrict"),
         InlineKeyboardButton("👋 الترحيب والمميزون", callback_data="help_welcome")],
    ])


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *قائمة الأوامر*\n\nاختر القسم اللي تبي تشوف أوامره:",
        parse_mode="MarkdownV2",
        reply_markup=_help_keyboard(),
    )


async def handle_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    back_btn = [[InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="help_main")]]

    if data == "help_main":
        await query.message.edit_text(
            "📋 *قائمة الأوامر*\n\nاختر القسم اللي تبي تشوف أوامره:",
            parse_mode="MarkdownV2",
            reply_markup=_help_keyboard(),
        )
        return

    if data == "help_admin":
        text = (
            "🔨 *أوامر الإدارة*\n"
            "_رد على رسالة العضو واكتب الأمر_\n\n"
            "• `حظر` — حظر عضو\n"
            "• `الغاء الحظر` — رفع الحظر عن عضو\n"
            "• `كتم` — كتم عضو\n"
            "• `الغاء الكتم` — رفع الكتم عن عضو\n"
            "• `طرد` — طرد عضو من المجموعة\n"
            "• `انذار` — إنذار عضو\n"
            "• `تثبيت` — تثبيت رسالة\n"
            "• `إلغاء تثبيت` — إلغاء تثبيت رسالة\n"
            "• `حذف` — حذف رسالة\n"
            "• `معلومات` — معلومات عن عضو\n"
            "• `الإحصائيات` — إحصائيات نشاط المجموعة\n"
            "• `إحصائياتي` — إحصائياتك الشخصية"
        )
    elif data == "help_owner":
        text = (
            "👑 *أوامر المالك فقط*\n\n"
            "• `رفع مشرف` — رفع عضو مشرفاً\n"
            "  _رد على رسالته أو اكتب @يوزرنيم_\n"
            "• `تنزيل عضو` — تنزيل مشرف إلى عضو عادي\n"
            "  _رد على رسالته أو اكتب @يوزرنيم_\n"
            "• `حد الرسائل` — تفعيل حد معين لرسائل عضو\n"
            "  _رد على رسالته أو اكتب @يوزرنيم_\n"
            "• `الغاء حد الرسائل` — إلغاء حد الرسائل عن عضو"
        )
    elif data == "help_study":
        text = (
            "🎯 *جلسات الدراسة \\(السشنات\\)*\n\n"
            "• `سشن` أو `بدء سشن` أو `اميرة سوي سشن` — بدء جلسة دراسة\n"
            "• `انهاء سشن` — إنهاء السشن _\\(رد على رسالة السشن\\)_\n"
            "  _للمشرف أو قائد السشن فقط_\n"
            "• `الانسحاب` أو `انسحاب` — الانسحاب من السشن الحالي\n"
            "• `انسحاب من السشن` — الانسحاب من السشن\n"
            "• `السشنات النشطة` — عرض السشنات الحالية مع أزرار إلغاء\n"
            "  _للمشرفين فقط_\n\n"
            "_البوت يحدد وقت الدراسة والاستراحة ويتابع المشاركين_"
        )
    elif data == "help_focus":
        text = (
            "🚫 *منع التسخيت*\n\n"
            "• `منع التسخيت` — تفعيل وضع التركيز لنفسك\n"
            "• `إيقاف منع التسخيت` — إيقاف وضع التركيز\n"
            "• `الغاء منع التسخيت` — نفس إيقاف منع التسخيت\n\n"
            "_لما تفعّله، البوت يراقب رسائلك ويحذّرك أو يحذفها_\n"
            "_تختار الوضع: تحذير أو حذف أو كتم_"
        )
    elif data == "help_replies":
        text = (
            "💬 *الردود التلقائية*\n"
            "_للمشرفين فقط_\n\n"
            "• `اضافة رد` — إضافة كلمة مفتاحية ورد تلقائي لها\n"
            "• `حذف رد [الكلمة]` — حذف رد تلقائي\n"
            "• `قائمة الردود` — عرض كل الردود التلقائية المضافة\n\n"
            "_الردود التلقائية تأخذ أولوية على ردود أميرة الافتراضية_"
        )
    elif data == "help_media":
        text = (
            "🎬 *الفيديو والصوت*\n\n"
            "• `اميرة شغلي [اسم الفيديو أو الأغنية]`\n"
            "  يبحث في يوتيوب ويعرض 5 نتائج،\n"
            "  ثم تختار تنزيل فيديو أو صوت فقط"
        )
    elif data == "help_bot":
        text = (
            "🤖 *التحدث مع أميرة*\n\n"
            "• اكتب `اميرة` أو `يا اميرة` متبوعاً بسؤالك\n"
            "• أو رد مباشرة على أي رسالة من أميرة\n\n"
            "📊 *الإحصائيات*\n\n"
            "• `الإحصائيات` — إحصائيات نشاط المجموعة\n"
            "• `إحصائياتي` — إحصائياتك الشخصية"
        )
    elif data == "help_filter":
        text = (
            "🛡 *فلتر الشتائم التلقائي*\n\n"
            "• يعمل تلقائياً بدون أي أمر\n"
            "• يحذف رسائل الشتائم فور وصولها\n"
            "• يشعر المشرفين بكل مخالفة\n"
            "• بعد 3 مخالفات يتم كتم العضو تلقائياً"
        )
    elif data == "help_restrict":
        text = (
            "📵 *تقييد الوسائط*\n"
            "_للمشرفين فقط — المشرفون والأعضاء المميزون مستثنون تلقائياً_\n\n"
            "*تعطيل نوع معين:*\n"
            "• `تعطيل الصور` / `تفعيل الصور`\n"
            "• `تعطيل الفيديو` / `تفعيل الفيديو`\n"
            "• `تعطيل الملفات` / `تفعيل الملفات`\n"
            "• `تعطيل الستيكر` / `تفعيل الستيكر`\n"
            "• `تعطيل الصوت` / `تفعيل الصوت`\n"
            "• `تعطيل الموسيقى` / `تفعيل الموسيقى`\n"
            "• `تعطيل الجيف` / `تفعيل الجيف`\n\n"
            "*تعطيل أو تفعيل الكل دفعة واحدة:*\n"
            "• `تعطيل الكل` — يقيّد جميع أنواع الوسائط\n"
            "• `تفعيل الكل` — يرفع جميع القيود"
        )
    elif data == "help_welcome":
        text = (
            "👋 *الترحيب والأعضاء المميزون*\n"
            "_للمشرفين فقط_\n\n"
            "*الترحيب بالأعضاء الجدد:*\n"
            "• `تفعيل الترحيب` — تشغيل رسائل الترحيب\n"
            "• `تعطيل الترحيب` — إيقاف رسائل الترحيب\n"
            "_البوت يرسل رسالة ترحيب عشوائية لكل عضو جديد_\n\n"
            "*الأعضاء المميزون:*\n"
            "_يتجاوزون جميع تقييدات الوسائط_\n"
            "• `رفع مميز` — رفع عضو إلى مميز _\\(رد على رسالته\\)_\n"
            "• `تنزيل مميز` — تنزيل عضو من المميزين _\\(رد على رسالته\\)_\n"
            "• `قائمة المميزين` — عرض قائمة المميزين"
        )
    else:
        return

    await query.message.edit_text(
        text,
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(back_btn),
    )


# ============================================================
# ⚙️ لوحة الإعدادات — للمالك فقط
# ============================================================



def is_ai_allowed_for_chat(chat_id: int) -> tuple:
    """يتحقق إذا كان الذكاء مسموحاً به. يعيد (bool, سبب_الرفض)."""
    if not _ai_enabled_chats.get(chat_id, True):
        return False, "disabled"
    limit = _ai_daily_limit.get(chat_id, 0)
    if limit == 0:
        return True, ""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _ai_daily_usage.get(chat_id, {"count": 0, "date": ""})
    if usage.get("date") != today:
        return True, ""
    if usage.get("count", 0) >= limit:
        return False, "limit"
    return True, ""


def increment_ai_usage(chat_id: int):
    """يزيد عداد استخدام الذكاء اليومي للمجموعة."""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _ai_daily_usage.get(chat_id, {"count": 0, "date": today})
    if usage.get("date") != today:
        usage = {"count": 0, "date": today}
    usage["count"] = usage.get("count", 0) + 1
    _ai_daily_usage[chat_id] = usage


def _mask_api_key(key: str) -> str:
    if len(key) > 12:
        return key[:6] + "***" + key[-4:]
    return key[:4] + "***"


def _build_api_keys_text() -> str:
    total = len(_gemini_api_keys)
    exhausted = len(_exhausted_key_indices)
    active = total - exhausted
    lines = [
        "🔑 *مفاتيح Gemini API*\n",
        f"📊 الإجمالي: {total} | ✅ نشطة: {active} | ❌ منتهية: {exhausted}",
    ]
    if total == 0:
        lines.append("\n_لا توجد مفاتيح مضافة_")
    return "\n".join(lines)


def _build_api_keys_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for i, key in enumerate(_gemini_api_keys):
        status = "❌" if i in _exhausted_key_indices else "✅"
        label = f"🗑 {_mask_api_key(key)}  {status}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"settings_del:{i}")])
    buttons.append([InlineKeyboardButton("➕ إضافة مفاتيح", callback_data="settings_add_keys")])
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")])
    return InlineKeyboardMarkup(buttons)


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أزرار لوحة الإعدادات — للمالك فقط."""
    global _history_enabled, _history_max_messages, _history_expiry_minutes, _max_sessions
    query = update.callback_query
    if query.from_user.id != OWNER_CHAT_ID:
        await query.answer("❌ غير مصرح لك.", show_alert=True)
        return

    data = query.data

    # ── القائمة الرئيسية ──
    if data == "settings_main":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 مفاتيح Gemini API", callback_data="settings_api_keys")],
            [InlineKeyboardButton("👥 مشرفو البوت", callback_data="settings_admins")],
            [InlineKeyboardButton("🏘 المجموعات النشطة", callback_data="settings_groups")],
            [InlineKeyboardButton("🤖 إعدادات الذكاء", callback_data="settings_ai")],
            [InlineKeyboardButton("💬 إعدادات حفظ الردود", callback_data="settings_history")],
            [InlineKeyboardButton("📚 إعدادات السشنات", callback_data="settings_sessions")],
        ])
        await query.message.edit_text(
            "⚙️ *الإعدادات*\n\nاختر ما تريد تعديله:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        await query.answer()
        return

    # ═══════════════════════════════════════
    # ── لوحة المشرفين ──
    # ═══════════════════════════════════════
    if data == "settings_admins":
        lines = ["👥 *مشرفو البوت*\n",
                 "_المشرفون يمكنهم استخدام البوت بالخاص_\n"]
        if _bot_admins:
            for uid in _bot_admins:
                lines.append(f"• `{uid}`")
        else:
            lines.append("_لا يوجد مشرفون مضافون حالياً_")
        rows = [[InlineKeyboardButton(f"🗑 حذف {uid}", callback_data=f"settings_del_adm:{uid}")] for uid in _bot_admins]
        rows.append([InlineKeyboardButton("➕ إضافة مشرف", callback_data="settings_add_admin")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")])
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
        await query.answer()
        return

    if data == "settings_add_admin":
        _pending_settings_input[query.from_user.id] = {"type": "add_admin"}
        await query.answer()
        await query.message.edit_text(
            "👥 *إضافة مشرف*\n\n"
            "أرسل **معرّف المستخدم** (User ID) — رقم مثل:\n`123456789`\n\n"
            "_(يمكنك معرفة ID أي شخص بالرد على رسالته بالأمر /info في المجموعة)_",
            parse_mode="Markdown",
        )
        return

    if data.startswith("settings_del_adm:"):
        uid = int(data.split(":")[1])
        _bot_admins.discard(uid)
        await query.answer("🗑 تم حذف المشرف.")
        lines = ["👥 *مشرفو البوت*\n",
                 "_المشرفون يمكنهم استخدام البوت بالخاص_\n"]
        if _bot_admins:
            for aid in _bot_admins:
                lines.append(f"• `{aid}`")
        else:
            lines.append("_لا يوجد مشرفون مضافون حالياً_")
        rows = [[InlineKeyboardButton(f"🗑 حذف {aid}", callback_data=f"settings_del_adm:{aid}")] for aid in _bot_admins]
        rows.append([InlineKeyboardButton("➕ إضافة مشرف", callback_data="settings_add_admin")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")])
        try:
            await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
        except Exception:
            pass
        return

    # ═══════════════════════════════════════
    # ── لوحة المجموعات ──
    # ═══════════════════════════════════════
    if data == "settings_groups":
        active = sorted(_owner_known_chats)
        lines = ["🏘 *المجموعات النشطة*\n"]
        if active:
            lines.append(f"📊 إجمالي المجموعات: *{len(active)}*\n")
        else:
            lines.append("_لا توجد مجموعات نشطة بعد_")
        buttons = []
        for cid in active:
            name = _known_chat_names.get(cid, str(cid))
            grp_keys_count = len(_group_gemini_keys.get(cid, []))
            label = f"🏘 {name}"
            if grp_keys_count:
                label += f" 🔑×{grp_keys_count}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"settings_grp_menu:{cid}")])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")])
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        await query.answer()
        return

    if data.startswith("settings_grp_menu:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        username = _known_chat_usernames.get(cid)
        grp_keys = _group_gemini_keys.get(cid, [])
        keys_line = f"🔑 مفاتيح خاصة: *{len(grp_keys)}* مفتاح" if grp_keys else "🔑 مفاتيح خاصة: لا توجد"
        ai_enabled = _ai_enabled_chats.get(cid, True)
        ai_line = "🤖 الذكاء: ✅ مفعّل" if ai_enabled else "🤖 الذكاء: ❌ موقف"
        rows = []
        if username:
            rows.append([InlineKeyboardButton("👁 معاينة المجموعة", url=f"https://t.me/{username}")])
        else:
            rows.append([InlineKeyboardButton("🆔 معرّف المجموعة", callback_data=f"settings_grp_id:{cid}")])
        rows.append([InlineKeyboardButton("🔑 إدارة مفاتيح الذكاء", callback_data=f"settings_grp_ai:{cid}")])
        rows.append([InlineKeyboardButton("➕ إضافة مفاتيح API مباشرة", callback_data=f"settings_grp_addkeys:{cid}")])
        rows.append([InlineKeyboardButton("⚙️ إعدادات الذكاء العامة", callback_data=f"settings_ai_c:{cid}")])
        rows.append([InlineKeyboardButton("🗑 إزالة المجموعة", callback_data=f"settings_grp_del:{cid}")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_groups")])
        await query.message.edit_text(
            f"🏘 *{name}*\n\n{keys_line}\n{ai_line}",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if data.startswith("settings_grp_id:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        await query.answer(f"{name}\nID: {cid}", show_alert=True)
        return

    if data.startswith("settings_grp_ai:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        grp_keys = _group_gemini_keys.get(cid, [])
        total_k = len(grp_keys)
        exhausted_k = len(_group_exhausted_keys.get(cid, set()))
        if total_k:
            keys_text = f"🔑 *{total_k}* مفتاح خاص"
            if exhausted_k:
                keys_text += f" ({exhausted_k} نفذ)"
            else:
                keys_text += " (كلهم مشحونين ✅)"
            preview_lines = []
            for i, k in enumerate(grp_keys):
                masked = k[:8] + "..." + k[-4:] if len(k) > 12 else k[:4] + "..."
                preview_lines.append(f"  {i+1}. `{masked}`")
            keys_text += "\n" + "\n".join(preview_lines)
        else:
            keys_text = "🔑 لا توجد مفاتيح خاصة\n_سيُستخدم المفاتيح الأساسية للبوت_"
        rows = [
            [InlineKeyboardButton("➕ إضافة مفاتيح خاصة", callback_data=f"settings_grp_addkeys:{cid}")],
        ]
        if grp_keys:
            rows.append([InlineKeyboardButton("🗑 حذف كل المفاتيح الخاصة", callback_data=f"settings_grp_clearkeys:{cid}")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"settings_grp_menu:{cid}")])
        await query.message.edit_text(
            f"🤖 *إدارة مفاتيح الذكاء*\n*{name}*\n\n{keys_text}",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if data.startswith("settings_grp_addkeys:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        _pending_group_api_key_input[query.from_user.id] = cid
        await query.answer()
        await query.message.edit_text(
            f"🔑 *إضافة مفاتيح لـ {name}*\n\n"
            "أرسل مفاتيح Gemini API — مفتاح واحد في كل سطر.\n\n"
            "_هذه المفاتيح ستُستخدم حصرياً لهذه المجموعة ولن تؤثر على المفاتيح الأساسية._",
            parse_mode="Markdown",
        )
        return

    if data.startswith("settings_grp_clearkeys:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        _group_gemini_keys.pop(cid, None)
        _group_exhausted_keys.pop(cid, None)
        save_data()
        await query.answer("✅ تم حذف المفاتيح الخاصة")
        grp_keys = _group_gemini_keys.get(cid, [])
        rows = [
            [InlineKeyboardButton("➕ إضافة مفاتيح خاصة", callback_data=f"settings_grp_addkeys:{cid}")],
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"settings_grp_menu:{cid}")],
        ]
        await query.message.edit_text(
            f"🤖 *إدارة مفاتيح الذكاء*\n*{name}*\n\n🔑 لا توجد مفاتيح خاصة\n_سيُستخدم المفاتيح الأساسية للبوت_",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    if data.startswith("settings_grp_del:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        rows = [
            [
                InlineKeyboardButton("✅ نعم، إزالة", callback_data=f"settings_grp_del_confirm:{cid}"),
                InlineKeyboardButton("❌ إلغاء", callback_data=f"settings_grp_menu:{cid}"),
            ]
        ]
        await query.message.edit_text(
            f"⚠️ *تأكيد الإزالة*\n\nهل تريد إزالة *{name}* من قائمة المجموعات النشطة؟",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if data.startswith("settings_grp_del_confirm:"):
        cid = int(data.split(":")[1])
        name = _known_chat_names.get(cid, str(cid))
        _owner_known_chats.discard(cid)
        _known_chat_names.pop(cid, None)
        _known_chat_usernames.pop(cid, None)
        _group_gemini_keys.pop(cid, None)
        _group_exhausted_keys.pop(cid, None)
        save_data()
        await query.answer(f"✅ تم إزالة {name}")
        active = sorted(_owner_known_chats)
        lines = ["🏘 *المجموعات النشطة*\n"]
        if active:
            lines.append(f"📊 إجمالي المجموعات: *{len(active)}*\n")
        else:
            lines.append("_لا توجد مجموعات نشطة بعد_")
        buttons = []
        for c in active:
            n = _known_chat_names.get(c, str(c))
            gk = len(_group_gemini_keys.get(c, []))
            lbl = f"🏘 {n}" + (f" 🔑×{gk}" if gk else "")
            buttons.append([InlineKeyboardButton(lbl, callback_data=f"settings_grp_menu:{c}")])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")])
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return

    # ═══════════════════════════════════════
    # ── لوحة إعدادات الذكاء ──
    # ═══════════════════════════════════════
    if data == "settings_ai":
        all_ai_chats = set(_owner_known_chats)
        lines = ["🤖 *إعدادات الذكاء الاصطناعي*\n"]
        rows = []
        if all_ai_chats:
            for cid in sorted(all_ai_chats):
                enabled = _ai_enabled_chats.get(cid, True)
                limit = _ai_daily_limit.get(cid, 0)
                today = datetime.now().strftime("%Y-%m-%d")
                used = _ai_daily_usage.get(cid, {})
                count = used.get("count", 0) if used.get("date") == today else 0
                status = "✅" if enabled else "❌"
                lim_str = f"{count}/{limit}" if limit > 0 else "∞"
                name = _known_chat_names.get(cid, str(cid))
                rows.append([InlineKeyboardButton(
                    f"{status} {name} — {lim_str}",
                    callback_data=f"settings_ai_c:{cid}"
                )])
        else:
            lines.append("_لا توجد مجموعات معروفة بعد_\n_(سيظهرون هنا بعد أول رسالة في المجموعة)_")
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")])
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
        await query.answer()
        return

    if data.startswith("settings_ai_c:"):
        cid = int(data.split(":")[1])
        enabled = _ai_enabled_chats.get(cid, True)
        limit = _ai_daily_limit.get(cid, 0)
        today = datetime.now().strftime("%Y-%m-%d")
        used = _ai_daily_usage.get(cid, {})
        count = used.get("count", 0) if used.get("date") == today else 0
        name = _known_chat_names.get(cid, str(cid))
        status_ar = "✅ مفعّل" if enabled else "❌ موقف"
        lim_ar = f"{limit} طلب/يوم (مستخدم اليوم: {count})" if limit > 0 else "بلا حد"
        toggle_lbl = "❌ إيقاف الذكاء" if enabled else "✅ تفعيل الذكاء"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_lbl, callback_data=f"settings_ai_tog:{cid}")],
            [InlineKeyboardButton("📊 تعيين حد يومي", callback_data=f"settings_ai_lim:{cid}")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_ai")],
        ])
        await query.message.edit_text(
            f"🤖 *إعدادات الذكاء*\n\n"
            f"المجموعة: *{name}*\n"
            f"الحالة: {status_ar}\n"
            f"الحد اليومي: {lim_ar}",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if data.startswith("settings_ai_tog:"):
        cid = int(data.split(":")[1])
        current = _ai_enabled_chats.get(cid, True)
        _ai_enabled_chats[cid] = not current
        save_data()
        state_ar = "مفعّل ✅" if not current else "موقف ❌"
        await query.answer(f"✅ الذكاء أصبح {state_ar}")
        enabled = _ai_enabled_chats[cid]
        limit = _ai_daily_limit.get(cid, 0)
        today = datetime.now().strftime("%Y-%m-%d")
        used = _ai_daily_usage.get(cid, {})
        count = used.get("count", 0) if used.get("date") == today else 0
        name = _known_chat_names.get(cid, str(cid))
        status_ar = "✅ مفعّل" if enabled else "❌ موقف"
        lim_ar = f"{limit} طلب/يوم (مستخدم اليوم: {count})" if limit > 0 else "بلا حد"
        toggle_lbl = "❌ إيقاف الذكاء" if enabled else "✅ تفعيل الذكاء"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_lbl, callback_data=f"settings_ai_tog:{cid}")],
            [InlineKeyboardButton("📊 تعيين حد يومي", callback_data=f"settings_ai_lim:{cid}")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_ai")],
        ])
        try:
            await query.message.edit_text(
                f"🤖 *إعدادات الذكاء*\n\n"
                f"المجموعة: *{name}*\n"
                f"الحالة: {status_ar}\n"
                f"الحد اليومي: {lim_ar}",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if data.startswith("settings_ai_lim:"):
        cid = int(data.split(":")[1])
        _pending_settings_input[query.from_user.id] = {"type": "set_limit", "chat_id": cid}
        await query.answer()
        name = _known_chat_names.get(cid, str(cid))
        await query.message.edit_text(
            f"📊 *تعيين الحد اليومي*\n\n"
            f"المجموعة: *{name}*\n\n"
            "أرسل عدد الطلبات المسموحة يومياً:\n"
            "_(أرسل 0 لإلغاء الحد تماماً)_",
            parse_mode="Markdown",
        )
        return

    # ══════════════════════════════════════════════
    # ── إعدادات حفظ الردود ──
    # ══════════════════════════════════════════════
    if data == "settings_history":
        tog_lbl = "✅ مفعّل — اضغط لإيقافه" if _history_enabled else "❌ موقوف — اضغط لتفعيله"
        rows = [
            [InlineKeyboardButton(tog_lbl, callback_data="settings_hist_tog")],
            [InlineKeyboardButton("📨 عدد الرسائل المحفوظة:", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 1 else ''}1", callback_data="settings_hist_n:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 2 else ''}2", callback_data="settings_hist_n:2"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 3 else ''}3", callback_data="settings_hist_n:3"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 5 else ''}5", callback_data="settings_hist_n:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 10 else ''}10", callback_data="settings_hist_n:10"),
            ],
            [InlineKeyboardButton("⏱ مدة الحفظ (دقائق):", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 1 else ''}1د", callback_data="settings_hist_exp:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 5 else ''}5د", callback_data="settings_hist_exp:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 10 else ''}10د", callback_data="settings_hist_exp:10"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 15 else ''}15د", callback_data="settings_hist_exp:15"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 30 else ''}30د", callback_data="settings_hist_exp:30"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 60 else ''}60د", callback_data="settings_hist_exp:60"),
            ],
            [InlineKeyboardButton("🗑 مسح كل التواريخ الآن", callback_data="settings_hist_clear")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")],
        ]
        status = "مفعّل ✅" if _history_enabled else "موقوف ❌"
        await query.message.edit_text(
            f"💬 *إعدادات حفظ الردود*\n\n"
            f"الحالة: {status}\n"
            f"عدد الرسائل المحفوظة: *{_history_max_messages}* رسالة\n"
            f"مدة الحفظ: *{_history_expiry_minutes}* دقيقة\n\n"
            f"_البوت يحفظ آخر {_history_max_messages} رسائل لكل شخص لمدة {_history_expiry_minutes} دقيقة حتى يفهم السياق._",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if data == "settings_hist_tog":
        _history_enabled = not _history_enabled
        save_data()
        await query.answer("✅ تم التفعيل" if _history_enabled else "❌ تم الإيقاف")
        # أعد عرض الصفحة
        tog_lbl = "✅ مفعّل — اضغط لإيقافه" if _history_enabled else "❌ موقوف — اضغط لتفعيله"
        rows = [
            [InlineKeyboardButton(tog_lbl, callback_data="settings_hist_tog")],
            [InlineKeyboardButton("📨 عدد الرسائل المحفوظة:", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 1 else ''}1", callback_data="settings_hist_n:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 2 else ''}2", callback_data="settings_hist_n:2"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 3 else ''}3", callback_data="settings_hist_n:3"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 5 else ''}5", callback_data="settings_hist_n:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 10 else ''}10", callback_data="settings_hist_n:10"),
            ],
            [InlineKeyboardButton("⏱ مدة الحفظ (دقائق):", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 1 else ''}1د", callback_data="settings_hist_exp:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 5 else ''}5د", callback_data="settings_hist_exp:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 10 else ''}10د", callback_data="settings_hist_exp:10"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 15 else ''}15د", callback_data="settings_hist_exp:15"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 30 else ''}30د", callback_data="settings_hist_exp:30"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 60 else ''}60د", callback_data="settings_hist_exp:60"),
            ],
            [InlineKeyboardButton("🗑 مسح كل التواريخ الآن", callback_data="settings_hist_clear")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")],
        ]
        status = "مفعّل ✅" if _history_enabled else "موقوف ❌"
        await query.message.edit_text(
            f"💬 *إعدادات حفظ الردود*\n\n"
            f"الحالة: {status}\n"
            f"عدد الرسائل المحفوظة: *{_history_max_messages}* رسالة\n"
            f"مدة الحفظ: *{_history_expiry_minutes}* دقيقة\n\n"
            f"_البوت يحفظ آخر {_history_max_messages} رسائل لكل شخص لمدة {_history_expiry_minutes} دقيقة حتى يفهم السياق._",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    if data.startswith("settings_hist_n:"):
        _history_max_messages = int(data.split(":")[1])
        save_data()
        await query.answer(f"✅ تم الضبط: {_history_max_messages} رسالة")
        tog_lbl = "✅ مفعّل — اضغط لإيقافه" if _history_enabled else "❌ موقوف — اضغط لتفعيله"
        rows = [
            [InlineKeyboardButton(tog_lbl, callback_data="settings_hist_tog")],
            [InlineKeyboardButton("📨 عدد الرسائل المحفوظة:", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 1 else ''}1", callback_data="settings_hist_n:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 2 else ''}2", callback_data="settings_hist_n:2"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 3 else ''}3", callback_data="settings_hist_n:3"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 5 else ''}5", callback_data="settings_hist_n:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 10 else ''}10", callback_data="settings_hist_n:10"),
            ],
            [InlineKeyboardButton("⏱ مدة الحفظ (دقائق):", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 1 else ''}1د", callback_data="settings_hist_exp:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 5 else ''}5د", callback_data="settings_hist_exp:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 10 else ''}10د", callback_data="settings_hist_exp:10"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 15 else ''}15د", callback_data="settings_hist_exp:15"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 30 else ''}30د", callback_data="settings_hist_exp:30"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 60 else ''}60د", callback_data="settings_hist_exp:60"),
            ],
            [InlineKeyboardButton("🗑 مسح كل التواريخ الآن", callback_data="settings_hist_clear")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")],
        ]
        status = "مفعّل ✅" if _history_enabled else "موقوف ❌"
        await query.message.edit_text(
            f"💬 *إعدادات حفظ الردود*\n\n"
            f"الحالة: {status}\n"
            f"عدد الرسائل المحفوظة: *{_history_max_messages}* رسالة\n"
            f"مدة الحفظ: *{_history_expiry_minutes}* دقيقة\n\n"
            f"_البوت يحفظ آخر {_history_max_messages} رسائل لكل شخص لمدة {_history_expiry_minutes} دقيقة حتى يفهم السياق._",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    if data.startswith("settings_hist_exp:"):
        _history_expiry_minutes = int(data.split(":")[1])
        await query.answer(f"✅ تم الضبط: {_history_expiry_minutes} دقيقة")
        tog_lbl = "✅ مفعّل — اضغط لإيقافه" if _history_enabled else "❌ موقوف — اضغط لتفعيله"
        rows = [
            [InlineKeyboardButton(tog_lbl, callback_data="settings_hist_tog")],
            [InlineKeyboardButton("📨 عدد الرسائل المحفوظة:", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 1 else ''}1", callback_data="settings_hist_n:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 2 else ''}2", callback_data="settings_hist_n:2"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 3 else ''}3", callback_data="settings_hist_n:3"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 5 else ''}5", callback_data="settings_hist_n:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_max_messages == 10 else ''}10", callback_data="settings_hist_n:10"),
            ],
            [InlineKeyboardButton("⏱ مدة الحفظ (دقائق):", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 1 else ''}1د", callback_data="settings_hist_exp:1"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 5 else ''}5د", callback_data="settings_hist_exp:5"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 10 else ''}10د", callback_data="settings_hist_exp:10"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 15 else ''}15د", callback_data="settings_hist_exp:15"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 30 else ''}30د", callback_data="settings_hist_exp:30"),
                InlineKeyboardButton(f"{'✅ ' if _history_expiry_minutes == 60 else ''}60د", callback_data="settings_hist_exp:60"),
            ],
            [InlineKeyboardButton("🗑 مسح كل التواريخ الآن", callback_data="settings_hist_clear")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")],
        ]
        status = "مفعّل ✅" if _history_enabled else "موقوف ❌"
        await query.message.edit_text(
            f"💬 *إعدادات حفظ الردود*\n\n"
            f"الحالة: {status}\n"
            f"عدد الرسائل المحفوظة: *{_history_max_messages}* رسالة\n"
            f"مدة الحفظ: *{_history_expiry_minutes}* دقيقة\n\n"
            f"_البوت يحفظ آخر {_history_max_messages} رسائل لكل شخص لمدة {_history_expiry_minutes} دقيقة حتى يفهم السياق._",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    if data == "settings_hist_clear":
        _user_history.clear()
        await query.answer("🗑 تم مسح كل تواريخ المحادثات.")
        return

    # ═══════════════════════════════════════
    # ── إعدادات السشنات ──
    # ═══════════════════════════════════════
    if data == "settings_sessions":
        active = sum(len(v) for v in _sessions.values())
        rows = [
            [InlineKeyboardButton("📊 الحد الأقصى للسشنات المتزامنة:", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 1 else ''}1", callback_data="settings_sess_max:1"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 2 else ''}2", callback_data="settings_sess_max:2"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 3 else ''}3", callback_data="settings_sess_max:3"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 5 else ''}5", callback_data="settings_sess_max:5"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 10 else ''}10", callback_data="settings_sess_max:10"),
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")],
        ]
        await query.message.edit_text(
            f"📚 *إعدادات السشنات*\n\n"
            f"الحد الأقصى للسشنات المتزامنة: *{_max_sessions}*\n"
            f"السشنات النشطة حالياً: *{active}*\n\n"
            f"_فقط قائد السشن أو مالك البوت يقدر يلغي السشن._",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if data.startswith("settings_sess_max:"):
        _max_sessions = int(data.split(":")[1])
        save_data()
        await query.answer(f"✅ الحد الأقصى صار {_max_sessions}")
        active = sum(len(v) for v in _sessions.values())
        rows = [
            [InlineKeyboardButton("📊 الحد الأقصى للسشنات المتزامنة:", callback_data="noop")],
            [
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 1 else ''}1", callback_data="settings_sess_max:1"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 2 else ''}2", callback_data="settings_sess_max:2"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 3 else ''}3", callback_data="settings_sess_max:3"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 5 else ''}5", callback_data="settings_sess_max:5"),
                InlineKeyboardButton(f"{'✅ ' if _max_sessions == 10 else ''}10", callback_data="settings_sess_max:10"),
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_main")],
        ]
        await query.message.edit_text(
            f"📚 *إعدادات السشنات*\n\n"
            f"الحد الأقصى للسشنات المتزامنة: *{_max_sessions}*\n"
            f"السشنات النشطة حالياً: *{active}*\n\n"
            f"_فقط قائد السشن أو مالك البوت يقدر يلغي السشن._",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    if data == "noop":
        await query.answer()
        return

    # ── عرض المفاتيح ──
    if data == "settings_api_keys":
        await query.message.edit_text(
            _build_api_keys_text(),
            reply_markup=_build_api_keys_keyboard(),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    # ── طلب إضافة مفاتيح ──
    if data == "settings_add_keys":
        _pending_api_key_input.add(query.from_user.id)
        await query.answer()
        await query.message.edit_text(
            "🔑 *إضافة مفاتيح API*\n\n"
            "أرسل المفاتيح الآن في رسالة واحدة، مفتاح واحد في كل سطر:\n\n"
            "_مثال:_\n`AIzaXXXXXXXXXXXX`\n`AIzaYYYYYYYYYYYY`",
            parse_mode="Markdown",
        )
        return

    # ── حذف مفتاح ──
    if data.startswith("settings_del:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(_gemini_api_keys):
            _gemini_api_keys.pop(idx)
            save_data()
            new_exhausted = set()
            for ei in _exhausted_key_indices:
                if ei < idx:
                    new_exhausted.add(ei)
                elif ei > idx:
                    new_exhausted.add(ei - 1)
            _exhausted_key_indices.clear()
            _exhausted_key_indices.update(new_exhausted)
            await query.answer("🗑 تم حذف المفتاح.")
        else:
            await query.answer("⚠️ المفتاح غير موجود.", show_alert=True)
            return

        try:
            await query.message.edit_text(
                _build_api_keys_text(),
                reply_markup=_build_api_keys_keyboard(),
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    await query.answer()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _owner_username
    user = update.effective_user
    if user and user.id == OWNER_CHAT_ID:
        if user.username:
            _owner_username = user.username
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings_main"),
        ]])
        await update.message.reply_text(
            "👋 أهلاً بك يا مالكي!\n\n"
            "أنا اميرة، بوتك لإدارة المجموعات 😊\n\n"
            "اضغط على الزر أدناه للوصول إلى الإعدادات.",
            reply_markup=keyboard,
        )
    else:
        owner_url = f"https://t.me/{_owner_username}" if _owner_username else None
        buttons = [[InlineKeyboardButton("📩 تواصل مع المالك", url=owner_url)]] if owner_url else []
        await update.message.reply_text(
            "👋 *أهلاً بك!*\n\n"
            "أنا اميرة، بوت مخصصة لإدارة المجموعات.\n\n"
            "إذا تريد تفعيل البوت في مجموعتك، "
            "اضف البوت للمجموعة وارفعه مشرف وسيتم التفعيل تلقائيا 😊",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            parse_mode="Markdown",
        )


async def do_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    if target.id == OWNER_CHAT_ID:
        await update.message.reply_text("🛡 لا يمكن تنفيذ الأمر على مالك البوت.")
        return
    chat_id = update.effective_chat.id
    if await is_admin_by_id(context, chat_id, target.id):
        await update.message.reply_text("⚠️ هذا مشرف، ما يصير تحظره.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status == "kicked":
            await update.message.reply_text(f"ℹ️ *{target.full_name}* محظور مسبقاً.", parse_mode="Markdown")
            return
    except TelegramError:
        pass
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        name = target.full_name
        await update.message.reply_text(f"🔨 تم حظر المستخدم *{name}* بنجاح.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل الحظر: {arabic_error(e)}")


async def do_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status != "kicked":
            await update.message.reply_text(f"ℹ️ *{target.full_name}* مو محظور أصلاً.", parse_mode="Markdown")
            return
    except TelegramError:
        pass
    try:
        await context.bot.unban_chat_member(chat_id, target.id)
        name = target.full_name
        await update.message.reply_text(f"✅ تم رفع الحظر عن المستخدم *{name}* بنجاح.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل رفع الحظر: {arabic_error(e)}")


async def do_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    if target.id == OWNER_CHAT_ID:
        await update.message.reply_text("🛡 لا يمكن تنفيذ الأمر على مالك البوت.")
        return
    chat_id = update.effective_chat.id
    if await is_admin_by_id(context, chat_id, target.id):
        await update.message.reply_text("⚠️ هذا مشرف، ما يصير تكتمه.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status == "restricted" and not member.can_send_messages:
            await update.message.reply_text(f"ℹ️ *{target.full_name}* مكتوم مسبقاً.", parse_mode="Markdown")
            return
    except TelegramError:
        pass
    try:
        await mute_user(context, chat_id, target.id)
        name = target.full_name
        await update.message.reply_text(f"🔇 تم كتم المستخدم *{name}* بنجاح.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل الكتم: {arabic_error(e)}")


async def do_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status != "restricted" or getattr(member, "can_send_messages", True):
            await update.message.reply_text(f"ℹ️ *{target.full_name}* مو مكتوم أصلاً.", parse_mode="Markdown")
            return
    except TelegramError:
        pass
    try:
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=True,
            can_pin_messages=False,
        )
        await context.bot.restrict_chat_member(chat_id, target.id, permissions)
        name = target.full_name
        await update.message.reply_text(f"🔊 تم رفع الكتم عن المستخدم *{name}* بنجاح.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل رفع الكتم: {arabic_error(e)}")


async def do_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    if target.id == OWNER_CHAT_ID:
        await update.message.reply_text("🛡 لا يمكن تنفيذ الأمر على مالك البوت.")
        return
    chat_id = update.effective_chat.id
    if await is_admin_by_id(context, chat_id, target.id):
        await update.message.reply_text("⚠️ هذا مشرف، ما يصير تطرده.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status in ("left", "kicked"):
            await update.message.reply_text(f"ℹ️ *{target.full_name}* مو موجود في المجموعة أصلاً.", parse_mode="Markdown")
            return
    except TelegramError:
        pass
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id)
        name = target.full_name
        await update.message.reply_text(f"👢 تم طرد المستخدم *{name}* من المجموعة.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل الطرد: {arabic_error(e)}")


async def do_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    if target.id == OWNER_CHAT_ID:
        await update.message.reply_text("🛡 لا يمكن تنفيذ الأمر على مالك البوت.")
        return
    chat_id_check = update.effective_chat.id
    if await is_admin_by_id(context, chat_id_check, target.id):
        await update.message.reply_text("⚠️ هذا مشرف، ما يصير تنذره.")
        return
    chat_id = update.effective_chat.id
    user_id = target.id
    key = f"{chat_id}_{user_id}"
    warn_data[key] = warn_data.get(key, 0) + 1
    count = warn_data[key]
    name = target.full_name
    if count >= 3:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            warn_data[key] = 0
            await update.message.reply_text(
                f"⚠️ تم تحذير المستخدم *{name}* للمرة الثالثة — تم حظره تلقائياً!",
                parse_mode="Markdown",
            )
        except TelegramError as e:
            await update.message.reply_text(f"❌ فشل الحظر التلقائي: {arabic_error(e)}")
    else:
        await update.message.reply_text(
            f"⚠️ تحذير للمستخدم *{name}*\n"
            f"عدد التحذيرات: {count}/3\n"
            f"عند الوصول إلى 3 تحذيرات سيتم الحظر تلقائياً.",
            parse_mode="Markdown",
        )


async def do_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❗ يرجى الرد على الرسالة المراد تثبيتها.")
        return
    chat_id = update.effective_chat.id
    message_id = update.message.reply_to_message.message_id
    try:
        await context.bot.pin_chat_message(chat_id, message_id)
        await update.message.reply_text("📌 تم تثبيت الرسالة بنجاح.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل التثبيت: {arabic_error(e)}")


async def do_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❗ يرجى الرد على الرسالة المراد إلغاء تثبيتها.")
        return
    chat_id = update.effective_chat.id
    message_id = update.message.reply_to_message.message_id
    try:
        await context.bot.unpin_chat_message(chat_id, message_id)
        await update.message.reply_text("📌 تم إلغاء تثبيت الرسالة.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل إلغاء التثبيت: {arabic_error(e)}")


async def do_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ أنت مو مشرف، هذا الأمر ما يخصك.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❗ يرجى الرد على الرسالة المراد حذفها.")
        return
    chat_id = update.effective_chat.id
    message_id = update.message.reply_to_message.message_id
    try:
        await context.bot.delete_message(chat_id, message_id)
        await update.message.delete()
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل الحذف: {arabic_error(e)}")


async def do_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_target_user_extended(update, context)
    if not target:
        target = update.effective_user
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        status_map = {
            "creator": "👑 مالك المجموعة",
            "administrator": "🛡 مشرف",
            "member": "👤 عضو",
            "restricted": "🔇 مقيّد",
            "left": "🚪 غادر المجموعة",
            "banned": "🔨 محظور",
        }
        status = status_map.get(member.status, member.status)
        username = f"@{target.username}" if target.username else "لا يوجد"
        vkey = f"{chat_id}_{target.id}"
        violations = profanity_violations.get(vkey, 0)
        info_text = (
            f"ℹ️ *معلومات المستخدم:*\n\n"
            f"👤 الاسم: *{target.full_name}*\n"
            f"🆔 المعرف: `{target.id}`\n"
            f"📛 اسم المستخدم: {username}\n"
            f"📊 الحالة: {status}\n"
            f"⚠️ مخالفات الشتائم: {violations}/3\n"
        )
        await update.message.reply_text(info_text, parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل جلب المعلومات: {arabic_error(e)}")


async def do_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رفع مشرف بدون صلاحيات — للمالك فقط."""
    if update.effective_user.id != OWNER_CHAT_ID:
        await update.message.reply_text("❌ هذا الأمر خاص بمالك المجموعة فقط.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ ارد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status in ("administrator", "creator"):
            await update.message.reply_text("⚠️ هذا العضو مشرف مسبقاً.")
            return
    except TelegramError:
        pass
    try:
        await context.bot.promote_chat_member(
            chat_id,
            target.id,
            can_manage_chat=True,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
        )
        name = target.full_name
        await update.message.reply_text(f"⭐ تم رفع *{name}* مشرفاً بنجاح.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل رفع المشرف: {arabic_error(e)}")


async def do_demote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنزيل مشرف إلى عضو — للمالك فقط."""
    if update.effective_user.id != OWNER_CHAT_ID:
        await update.message.reply_text("❌ هذا الأمر خاص بمالك المجموعة فقط.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ ارد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status == "creator":
            await update.message.reply_text("⚠️ ما يصير تنزل مالك المجموعة.")
            return
        if member.status != "administrator":
            await update.message.reply_text("⚠️ هذا العضو مو مشرف أصلاً.")
            return
    except TelegramError:
        pass
    try:
        await context.bot.promote_chat_member(
            chat_id,
            target.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
        )
        name = target.full_name
        await update.message.reply_text(f"🔽 تم تنزيل *{name}* إلى عضو.", parse_mode="Markdown")
    except TelegramError as e:
        await update.message.reply_text(f"❌ فشل تنزيل المشرف: {arabic_error(e)}")


# ============================================================
# 📊 نظام حد الرسائل
# ============================================================

def _format_duration(seconds: int) -> str:
    if seconds < 3600:
        m = seconds // 60
        return f"{m} دقيقة"
    elif seconds < 86400:
        h = seconds // 3600
        rem = (seconds % 3600) // 60
        if rem == 0:
            return f"{h} ساعة" if h == 1 else f"{h} ساعات"
        return f"{h} ساعة و{rem} دقيقة"
    else:
        d = seconds // 86400
        return f"{d} يوم" if d == 1 else f"{d} أيام"


def _build_rl_count_keyboard(target_id: int, chat_id: int) -> InlineKeyboardMarkup:
    counts = [5, 10, 20, 30, 50, 100]
    buttons = []
    row = []
    for c in counts:
        row.append(InlineKeyboardButton(str(c), callback_data=f"rl_c_{target_id}_{chat_id}_{c}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ مخصص", callback_data=f"rl_c_{target_id}_{chat_id}_x")])
    return InlineKeyboardMarkup(buttons)


def _build_rl_time_keyboard(target_id: int, chat_id: int, count: int) -> InlineKeyboardMarkup:
    times = [
        ("30 دقيقة", 1800), ("1 ساعة", 3600), ("2 ساعة", 7200),
        ("3 ساعات", 10800), ("6 ساعات", 21600), ("12 ساعة", 43200), ("24 ساعة", 86400),
    ]
    buttons = []
    row = []
    for label, secs in times:
        row.append(InlineKeyboardButton(label, callback_data=f"rl_t_{target_id}_{chat_id}_{count}_{secs}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ مخصص (بالدقائق)", callback_data=f"rl_t_{target_id}_{chat_id}_{count}_x")])
    return InlineKeyboardMarkup(buttons)


def _activate_rate_limit_data(chat_id: int, target_id: int, target_name: str, count: int, window_seconds: int) -> str:
    rl_key = f"{chat_id}_{target_id}"
    _rate_limits[rl_key] = {
        "limit": count,
        "window_seconds": window_seconds,
        "count": 0,
        "reset_time": datetime.now() + timedelta(seconds=window_seconds),
        "restricted": False,
        "was_admin": False,
        "target_name": target_name,
    }
    time_str = _format_duration(window_seconds)
    return (
        f"✅ تم تفعيل حد الرسائل لـ *{target_name}*\n"
        f"✉️ الحد: {count} رسالة\n"
        f"⏱ المدة: {time_str}\n\n"
        f"_سيُقيَّد تلقائياً عند تجاوز الحد._"
    )


async def _restore_rate_limit_task(bot, chat_id: int, user_id: int, window_seconds: int, was_admin: bool):
    await asyncio.sleep(window_seconds)
    rl_key = f"{chat_id}_{user_id}"
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception:
        pass
    if was_admin:
        try:
            await bot.promote_chat_member(
                chat_id, user_id,
                can_manage_chat=True,
                can_delete_messages=True,
                can_restrict_members=True,
                can_promote_members=False,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=True,
            )
        except Exception:
            pass
    # أعد تهيئة العداد للنافذة الجديدة — حد الرسائل يبقى نشطاً حتى يُلغى يدوياً
    if rl_key in _rate_limits:
        rl = _rate_limits[rl_key]
        rl["count"] = 0
        rl["restricted"] = False
        rl["reset_time"] = datetime.now() + timedelta(seconds=window_seconds)


async def _apply_rate_limit_restriction(bot, chat_id: int, user_id: int, user_name: str, window_seconds: int):
    if user_id == OWNER_CHAT_ID:
        return
    rl_key = f"{chat_id}_{user_id}"
    rl = _rate_limits.get(rl_key)
    if not rl or rl.get("restricted"):
        return
    rl["restricted"] = True
    was_admin = False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in ("administrator", "creator"):
            was_admin = True
            if member.status != "creator":
                await bot.promote_chat_member(
                    chat_id, user_id,
                    can_manage_chat=False,
                    can_delete_messages=False,
                    can_manage_video_chats=False,
                    can_restrict_members=False,
                    can_promote_members=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                )
    except Exception:
        pass
    rl["was_admin"] = was_admin
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            ChatPermissions(
                can_send_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
        )
    except Exception:
        pass
    time_str = _format_duration(window_seconds)
    user_mention = f"[{user_name}](tg://user?id={user_id})"
    try:
        await bot.send_message(
            chat_id,
            f"🔇 *{user_mention}* تجاوز حد الرسائل ({rl['limit']} رسالة).\n"
            f"⏳ سيُمنع من الإرسال لمدة {time_str}.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    asyncio.create_task(_restore_rate_limit_task(bot, chat_id, user_id, window_seconds, was_admin))


async def do_rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await _is_group_creator(context, chat_id, update.effective_user.id):
        await update.message.reply_text("❌ هذا الأمر خاص بمالك المجموعة فقط.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    chat_id = update.effective_chat.id
    kb = _build_rl_count_keyboard(target.id, chat_id)
    await update.message.reply_text(
        f"📊 *حد الرسائل لـ {target.full_name}*\n\n"
        f"اختر عدد الرسائل المسموح بها قبل التقييد:",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def do_cancel_rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await _is_group_creator(context, chat_id, update.effective_user.id):
        await update.message.reply_text("❌ هذا الأمر خاص بمالك المجموعة فقط.")
        return
    target = await get_target_user_extended(update, context)
    if not target:
        await update.message.reply_text("❗ رد على رسالة العضو أو اكتب @يوزرنيم بعد الأمر.")
        return
    chat_id = update.effective_chat.id
    rl_key = f"{chat_id}_{target.id}"
    if rl_key not in _rate_limits:
        await update.message.reply_text(f"⚠️ لا يوجد حد رسائل مفعّل لـ *{target.full_name}*.", parse_mode="Markdown")
        return
    del _rate_limits[rl_key]
    try:
        await context.bot.restrict_chat_member(
            chat_id, target.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception:
        pass
    user_mention = f"[{target.full_name}](tg://user?id={target.id})"
    await update.message.reply_text(
        f"✅ تم إلغاء حد الرسائل عن {user_mention}.",
        parse_mode="Markdown",
    )


async def handle_rate_limit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    owner_id = update.effective_user.id

    # استخرج chat_id من البيانات للتحقق من أن المستخدم مالك تلك المجموعة
    _rl_parts_check = data[5:].split("_") if (data.startswith("rl_c_") or data.startswith("rl_t_")) else []
    _rl_chat_id_check = int(_rl_parts_check[1]) if len(_rl_parts_check) >= 2 else None
    if _rl_chat_id_check and not await _is_group_creator(context, _rl_chat_id_check, owner_id):
        return

    if data.startswith("rl_c_"):
        rest = data[5:]
        parts = rest.split("_")
        target_id = int(parts[0])
        chat_id = int(parts[1])
        val_str = parts[2]
        if val_str == "x":
            _pending_settings_input[owner_id] = {
                "type": "rl_custom_count",
                "target_id": target_id,
                "chat_id": chat_id,
            }
            await query.edit_message_text("✏️ أرسل عدد الرسائل المسموح بها (رقم من 1 إلى 9999):")
        else:
            count = int(val_str)
            try:
                member = await context.bot.get_chat_member(chat_id, target_id)
                target_name = member.user.full_name
            except Exception:
                target_name = str(target_id)
            kb = _build_rl_time_keyboard(target_id, chat_id, count)
            await query.edit_message_text(
                f"📊 *حد الرسائل لـ {target_name}*\n"
                f"✉️ الحد: {count} رسالة\n\n"
                f"⏱ اختر المدة الزمنية:",
                reply_markup=kb,
                parse_mode="Markdown",
            )

    elif data.startswith("rl_t_"):
        rest = data[5:]
        parts = rest.split("_")
        target_id = int(parts[0])
        chat_id = int(parts[1])
        count = int(parts[2])
        val_str = parts[3]
        if val_str == "x":
            _pending_settings_input[owner_id] = {
                "type": "rl_custom_time",
                "target_id": target_id,
                "chat_id": chat_id,
                "count": count,
            }
            await query.edit_message_text(
                f"✉️ الحد: {count} رسالة\n\n"
                f"✏️ أرسل المدة بالدقائق (مثال: 90 = ساعة ونصف، 1440 = يوم):"
            )
        else:
            secs = int(val_str)
            try:
                member = await context.bot.get_chat_member(chat_id, target_id)
                target_name = member.user.full_name
            except Exception:
                target_name = str(target_id)
            msg = _activate_rate_limit_data(chat_id, target_id, target_name, count, secs)
            await query.edit_message_text(msg, parse_mode="Markdown")


# ============================================================
# 🎯 دوال السشنات
# ============================================================

def build_mentions(participants: list) -> str:
    """يبني نص منشن لكل المشاركين (Markdown v1 — محجوز للرسائل القديمة)."""
    parts = []
    for p in participants:
        if p.get("username"):
            parts.append(f"@{p['username']}")
        else:
            parts.append(f"[{p['name']}](tg://user?id={p['id']})")
    return " ".join(parts) if parts else ""


def build_mentions_html(participants: list) -> str:
    """يبني نص منشن بصيغة HTML — آمن ضد أي أحرف خاصة."""
    parts = []
    for p in participants:
        if p.get("username"):
            parts.append(f"@{p['username']}")
        else:
            safe_name = _html_module.escape(p["name"])
            parts.append(f'<a href="tg://user?id={p["id"]}">{safe_name}</a>')
    return " ".join(parts) if parts else ""


def build_session_message(chat_id: int, sess_id: int) -> tuple:
    """يبني رسالة السشن بصيغة HTML حسب المرحلة (waiting / studying)."""
    session = _sessions[chat_id][sess_id]
    participants = session["participants"]
    session_num = session.get("session_num", 1)
    phase = session.get("phase", "waiting")
    names = " | ".join(_html_module.escape(p["name"]) for p in participants) if participants else "لا أحد بعد"
    ordinal_f = _session_ordinal_f(session_num)
    creator = _html_module.escape(session["creator_name"])
    study = session["study"]
    break_t = session["break"]
    count = len(participants)
    mentions = build_mentions_html(participants)

    if phase == "waiting":
        text = (
            f"🎯 <b>جلسة الدراسة {ordinal_f}!</b>\n\n"
            f"👤 المنظم: {creator}\n"
            f"📚 الدراسة: <b>{study}</b> دقيقة\n"
            f"☕ الاستراحة: <b>{break_t}</b> دقيقة\n"
            f"👥 المشاركون ({count}): {names}"
        )
        rows = [
            [InlineKeyboardButton("✋ انضم للسشن", callback_data=f"sess_join:{chat_id}:{sess_id}")],
            [InlineKeyboardButton("🚀 بدء السشن", callback_data=f"sess_start:{chat_id}:{sess_id}")],
        ]
    else:
        text = (
            f"📚 <b>جلسة الدراسة {ordinal_f} — نشطة!</b>\n\n"
            f"👤 المنظم: {creator}\n"
            f"⏱ الدراسة: <b>{study}</b> دقيقة\n"
            f"☕ الاستراحة: <b>{break_t}</b> دقيقة\n"
            f"👥 المشاركون ({count}): {names}\n\n"
            f"🔴 <i>جارٍ الدراسة...</i>\n\n"
            f"{mentions}"
        )
        rows = [
            [InlineKeyboardButton("✋ انضم للسشن", callback_data=f"sess_join:{chat_id}:{sess_id}")],
        ]
    return text, InlineKeyboardMarkup(rows)


async def run_session_timer(chat_id: int, sess_id: int, bot, elapsed_seconds: int = 0):
    """مؤقت السشن: ينبّه عند انتهاء الدراسة ثم الاستراحة."""
    try:
        if chat_id not in _sessions or sess_id not in _sessions[chat_id]:
            return
        session = _sessions[chat_id][sess_id]
        study_min = session["study"]
        break_min = session["break"]
        session_num = session.get("session_num", 1)

        # ── انتظار وقت الدراسة (مع مراعاة الوقت المنقضي عند الاستعادة) ──
        await asyncio.sleep(max(0, study_min * 60 - elapsed_seconds))
        if chat_id not in _sessions or sess_id not in _sessions[chat_id]:
            return
        session = _sessions[chat_id][sess_id]
        mentions = build_mentions_html(session["participants"])
        names = " | ".join(_html_module.escape(p["name"]) for p in session["participants"])
        creator = _html_module.escape(session["creator_name"])

        # حذف البطاقة النشطة وإرسال رسالة انتهاء الدراسة
        old_msg = session.get("message_id")
        if old_msg:
            try:
                await bot.delete_message(chat_id, old_msg)
            except Exception:
                pass
        session["present_users"] = set()
        session["present_expires_at"] = datetime.now() + timedelta(minutes=30)
        join_present_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✋ انضم للسشن", callback_data=f"sess_join:{chat_id}:{sess_id}")],
            [InlineKeyboardButton("✅ انا موجود", callback_data=f"sess_present:{chat_id}:{sess_id}")],
        ])
        study_end_msg = await bot.send_message(
            chat_id,
            f"🎉🎊🥳 <b>أحسنتم! انتهت جلسة الدراسة {_session_ordinal_f(session_num)}!</b>\n\n"
            f"👤 المنظم: {creator}\n"
            f"👥 المشاركون ({len(session['participants'])}): {names}\n\n"
            f"استحقيتوا راحة <b>{break_min}</b> دقيقة ☕\n\n"
            f"{mentions}\n\n"
            f"💡 اضغط <b>انا موجود</b> لتسجيل مشاركتك في الإحصائيات",
            reply_markup=join_present_kb,
            parse_mode="HTML",
        )
        session["message_id"] = study_end_msg.message_id

        # ── انتظار وقت الاستراحة ──
        await asyncio.sleep(break_min * 60)
        if chat_id not in _sessions or sess_id not in _sessions[chat_id]:
            return
        session = _sessions[chat_id][sess_id]
        mentions = build_mentions_html(session["participants"])
        names = " | ".join(_html_module.escape(p["name"]) for p in session["participants"])
        next_num = session_num + 1
        next_ord = _session_ordinal(next_num)

        # حفظ بيانات السشن القادم
        _pending_next_session[(chat_id, sess_id)] = {
            "study": study_min,
            "break": break_min,
            "participants": list(session["participants"]),
            "creator_id": session["creator_id"],
            "creator_name": session["creator_name"],
            "next_num": next_num,
        }

        # حذف رسالة انتهاء الدراسة وإرسال رسالة انتهاء الاستراحة
        old_msg2 = session.get("message_id")
        if old_msg2:
            try:
                await bot.delete_message(chat_id, old_msg2)
            except Exception:
                pass
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🚀 بدء السشن {next_ord}", callback_data=f"sess_next:{chat_id}:{sess_id}"),
        ]])
        participant_lines = "\n".join(
            f"• {_html_module.escape(p['name'])}" + (f" @{p['username']}" if p.get('username') else "")
            for p in session["participants"]
        )
        await bot.send_message(
            chat_id,
            f"☕ <b>انتهت الاستراحة!</b>\n\n"
            f"👥 <b>المشاركون</b>\n{participant_lines}\n\n"
            f"مستعدون للسشن {next_ord}؟ 💪\n\n"
            f"{mentions}",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

        # حذف السشن القديم
        _db_delete_session(chat_id, sess_id)
        _sessions[chat_id].pop(sess_id, None)
        if not _sessions.get(chat_id):
            _sessions.pop(chat_id, None)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"خطأ في مؤقت السشن {sess_id} للمجموعة {chat_id}: {e}")


async def show_session_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض قائمة اختيار مدة السشن."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    group_sessions = _sessions.get(chat_id, {})
    if user_id and _user_has_active_session(chat_id, user_id):
        await update.message.reply_text(
            "⚠️ لديك سشن نشط بالفعل في هذه المجموعة.\n"
            "أنهِ سشنك الحالي أولاً قبل بدء سشن جديد."
        )
        return
    if len(group_sessions) >= _max_sessions:
        await update.message.reply_text(
            f"عذراً، يوجد {len(group_sessions)} سشن نشط حالياً في هذه المجموعة 🚫\n"
            f"انتظر انتهاء أحد السشنات ثم حاول مجدداً."
        )
        return
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 25 / 5 دقيقة", callback_data="sess_p:25:5"),
            InlineKeyboardButton("⏱ 45 / 15 دقيقة", callback_data="sess_p:45:15"),
        ],
        [
            InlineKeyboardButton("⏱ 50 / 10 دقيقة", callback_data="sess_p:50:10"),
            InlineKeyboardButton("⏱ 90 / 20 دقيقة", callback_data="sess_p:90:20"),
        ],
        [
            InlineKeyboardButton("⚙️ تخصيص", callback_data="sess_custom"),
        ],
    ])
    text = (
        "🎯 *إنشاء جلسة دراسة*\n\n"
        "اختر مدة الدراسة والاستراحة:\n"
        "_(دراسة / استراحة — بالدقائق)_"
    )
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


def _user_has_active_session(chat_id: int, user_id: int) -> bool:
    """يتحقق إذا كان المستخدم منشئاً أو مشاركاً في أي سشن نشط في هذه المجموعة."""
    for sess in _sessions.get(chat_id, {}).values():
        if sess.get("creator_id") == user_id:
            return True
        if any(p["id"] == user_id for p in sess.get("participants", [])):
            return True
    return False


def _create_session(chat_id: int, study: int, break_t: int, creator_id: int,
                    creator_name: str, creator_username: str, session_num: int = 1,
                    extra_participants: list = None) -> int:
    """ينشئ سشناً جديداً ويعيد sess_id."""
    _session_counters[chat_id] = _session_counters.get(chat_id, 0) + 1
    sess_id = _session_counters[chat_id]
    if chat_id not in _sessions:
        _sessions[chat_id] = {}
    now = datetime.now()
    participants = [{"id": creator_id, "name": creator_name, "username": creator_username, "joined_at": None}]
    if extra_participants:
        for p in extra_participants:
            if p["id"] != creator_id:
                participants.append({"id": p["id"], "name": p["name"], "username": p.get("username", ""), "joined_at": None})
    _sessions[chat_id][sess_id] = {
        "study": study,
        "break": break_t,
        "participants": participants,
        "creator_name": creator_name,
        "creator_id": creator_id,
        "message_id": None,
        "task": None,
        "sess_id": sess_id,
        "session_num": session_num,
        "phase": "waiting",
        "started_at": None,
    }
    _db_save_session(chat_id, sess_id)
    return sess_id


async def handle_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أزرار السشن: اختيار مدة، تخصيص، انضمام، بدء السشن القادم."""
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat.id
    user = query.from_user

    # ── اختيار مدة جاهزة ──
    if data.startswith("sess_p:"):
        parts = data.split(":")
        study = int(parts[1])
        break_t = int(parts[2])
        # فحص: هل للمستخدم سشن نشط بالفعل في هذه المجموعة؟
        if _user_has_active_session(chat_id, user.id):
            await query.answer("⚠️ لديك سشن نشط بالفعل، أنهِه أولاً.", show_alert=True)
            return
        # فحص الحد لهذه المجموعة
        if len(_sessions.get(chat_id, {})) >= _max_sessions:
            await query.answer(f"❌ وصلنا للحد الأقصى ({_max_sessions} سشن) في هذه المجموعة.", show_alert=True)
            return
        sess_id = _create_session(chat_id, study, break_t, user.id, user.first_name, user.username or "")
        try:
            await query.message.delete()
        except Exception:
            pass
        text, keyboard = build_session_message(chat_id, sess_id)
        msg = await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
        _sessions[chat_id][sess_id]["message_id"] = msg.message_id
        await query.answer("✅ اشترك وابدأ السشن!")
        return

    # ── تخصيص المدة — إدخال نصي ──
    if data == "sess_custom":
        _pending_session_config[user.id] = {"step": "study", "chat_id": chat_id}
        await query.answer()
        await query.message.edit_text(
            "⚙️ <b>تخصيص السشن</b>\n\n"
            "📝 أرسل مدة الدراسة بالدقائق (مثال: 25)",
            parse_mode="HTML",
        )
        return

    # ── الانضمام للسشن ──
    if data.startswith("sess_join:"):
        parts = data.split(":")
        tgt_chat = int(parts[1])
        tgt_sess = int(parts[2])
        if tgt_chat not in _sessions or tgt_sess not in _sessions[tgt_chat]:
            await query.answer("❌ السشن انتهى أو لم يبدأ بعد.", show_alert=True)
            return
        session = _sessions[tgt_chat][tgt_sess]
        if any(p["id"] == user.id for p in session["participants"]):
            await query.answer("✅ أنت مشارك بالفعل!", show_alert=True)
            return
        # منع الانضمام لأكثر من سشن في نفس الوقت
        if _user_has_active_session(tgt_chat, user.id):
            await query.answer("⚠️ أنت مشارك بالفعل في سشن آخر في هذه المجموعة. اغادر سشنك الحالي أولاً.", show_alert=True)
            return
        # تسجيل وقت الانضمام (None إذا لم يبدأ السشن بعد، وقت الانضمام الفعلي إذا بدأ)
        joined_at = datetime.now() if session.get("started_at") else None
        session["participants"].append({"id": user.id, "name": user.first_name, "username": user.username or "", "joined_at": joined_at})
        try:
            await query.message.delete()
        except Exception:
            pass
        text, keyboard = build_session_message(tgt_chat, tgt_sess)
        msg = await context.bot.send_message(tgt_chat, text, reply_markup=keyboard, parse_mode="HTML")
        session["message_id"] = msg.message_id
        _db_save_session(tgt_chat, tgt_sess)
        await query.answer("✅ انضممت للسشن!")
        return

    # ── تسجيل الحضور للإحصائيات ──
    if data.startswith("sess_present:"):
        parts = data.split(":")
        tgt_chat = int(parts[1])
        tgt_sess = int(parts[2])
        session = (_sessions.get(tgt_chat) or {}).get(tgt_sess)
        if not session:
            await query.answer("❌ انتهى السشن، لم يتم تسجيل حضورك.", show_alert=True)
            return
        # فحص انتهاء مهلة الـ 30 دقيقة
        expires_at = session.get("present_expires_at")
        if expires_at and datetime.now() > expires_at:
            await query.answer("⏰ انتهت مهلة تسجيل الحضور (30 دقيقة).", show_alert=True)
            return
        # فحص: فقط المشاركون في السشن يقدرون يسجلون حضورهم
        participant_ids = {p["id"] for p in session.get("participants", [])}
        if user.id not in participant_ids:
            await query.answer("❌ فقط المشاركون في السشن يقدرون يسجلون حضورهم.", show_alert=True)
            return
        present_users = session.setdefault("present_users", set())
        if user.id in present_users:
            await query.answer("✅ تم تسجيل حضورك مسبقاً!", show_alert=True)
            return
        present_users.add(user.id)
        # تسجيل في الإحصائيات مع حساب الوقت الفعلي من لحظة الانضمام
        full_study_min = session.get("study", 0)
        started_at = session.get("started_at")
        # ابحث عن وقت انضمام هذا المستخدم تحديداً
        participant_joined_at = None
        for p in session.get("participants", []):
            if p["id"] == user.id:
                participant_joined_at = p.get("joined_at")
                break
        if started_at and participant_joined_at:
            elapsed_before_join = (participant_joined_at - started_at).total_seconds() / 60
            effective_study_min = max(0, int(full_study_min - elapsed_before_join))
        else:
            effective_study_min = full_study_min
        if tgt_chat not in _session_stats:
            _session_stats[tgt_chat] = {}
        stats = _session_stats[tgt_chat]
        if user.id not in stats:
            stats[user.id] = {"name": user.first_name, "username": user.username or "", "sessions": 0, "study_minutes": 0}
        stats[user.id]["name"] = user.first_name
        stats[user.id]["username"] = user.username or ""
        stats[user.id]["sessions"] += 1
        stats[user.id]["study_minutes"] += effective_study_min
        # سجل يومي للفلترة الزمنية
        today_str = datetime.now().strftime("%Y-%m-%d")
        log_entry = {"date": today_str, "study_minutes": effective_study_min}
        stats[user.id].setdefault("log", []).append(log_entry)
        save_data()
        await query.answer(f"✅ تم تسجيل حضورك! ({effective_study_min} دقيقة دراسة)", show_alert=True)
        return

    # ── بدء السشن — عدّ تنازلي ──
    if data.startswith("sess_start:"):
        parts = data.split(":")
        tgt_chat = int(parts[1])
        tgt_sess = int(parts[2])
        if tgt_chat not in _sessions or tgt_sess not in _sessions[tgt_chat]:
            await query.answer("❌ السشن انتهى أو لم يعد موجوداً.", show_alert=True)
            return
        session = _sessions[tgt_chat][tgt_sess]
        # فحص: صاحب السشن فقط يقدر يبدأه
        if user.id != session.get("creator_id"):
            await query.answer("❌ فقط منظم السشن يقدر يبدأه.", show_alert=True)
            return
        # منع بدء مزدوج
        existing_task = session.get("task")
        if existing_task and not existing_task.done():
            await query.answer("⚡ السشن بدأ بالفعل!", show_alert=True)
            return
        await query.answer()
        study_min = session["study"]
        break_min = session["break"]
        session_num = session.get("session_num", 1)
        ordinal_f = _session_ordinal_f(session_num)
        # العدّ التنازلي — رسالة منفصلة
        cd = await context.bot.send_message(tgt_chat, "3️⃣")
        await asyncio.sleep(1)
        await cd.edit_text("2️⃣")
        await asyncio.sleep(1)
        await cd.edit_text("1️⃣")
        await asyncio.sleep(1)
        # حذف بطاقة الانتظار الأصلية
        old_msg_id = session.get("message_id")
        if old_msg_id:
            try:
                await context.bot.delete_message(tgt_chat, old_msg_id)
            except Exception:
                pass
        # تسجيل وقت بدء السشن وتحديث joined_at لكل المشاركين الحاليين
        session["started_at"] = datetime.now()
        for p in session["participants"]:
            if p.get("joined_at") is None:
                p["joined_at"] = session["started_at"]
        # تحويل رسالة العدّ إلى بطاقة السشن النشط
        session["phase"] = "studying"
        names = " | ".join(_html_module.escape(p["name"]) for p in session["participants"])
        creator = _html_module.escape(session["creator_name"])
        mentions = build_mentions_html(session["participants"])
        active_text = (
            f"📚 <b>جلسة الدراسة {ordinal_f} — نشطة!</b>\n\n"
            f"👤 المنظم: {creator}\n"
            f"⏱ الدراسة: <b>{study_min}</b> دقيقة\n"
            f"☕ الاستراحة: <b>{break_min}</b> دقيقة\n"
            f"👥 المشاركون ({len(session['participants'])}): {names}\n\n"
            f"🔴 <i>جارٍ الدراسة...</i>\n\n"
            f"{mentions}"
        )
        join_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✋ انضم للسشن", callback_data=f"sess_join:{tgt_chat}:{tgt_sess}"),
        ]])
        await cd.edit_text(active_text, reply_markup=join_kb, parse_mode="HTML")
        session["message_id"] = cd.message_id
        # بدء التايمر
        task = asyncio.create_task(run_session_timer(tgt_chat, tgt_sess, context.bot))
        _sessions[tgt_chat][tgt_sess]["task"] = task
        _db_save_session(tgt_chat, tgt_sess)
        return

    # ── بدء السشن القادم ──
    if data.startswith("sess_next:"):
        parts = data.split(":")
        orig_chat = int(parts[1])
        orig_sess = int(parts[2])
        pending = _pending_next_session.get((orig_chat, orig_sess))
        if not pending:
            await query.answer("❌ انتهت صلاحية هذا الزر.", show_alert=True)
            return
        # فحص: صاحب السشن فقط يقدر يبدأ السشن التالي
        if user.id != pending.get("creator_id"):
            await query.answer("❌ فقط منظم السشن يقدر يبدأ السشن التالي.", show_alert=True)
            return
        _pending_next_session.pop((orig_chat, orig_sess), None)
        if len(_sessions.get(orig_chat, {})) >= _max_sessions:
            await query.answer(f"❌ وصلنا للحد الأقصى ({_max_sessions} سشن) في هذه المجموعة.", show_alert=True)
            return
        next_num = pending["next_num"]
        next_ord = _session_ordinal(next_num)
        # إنشاء السشن الجديد بنفس المشاركين السابقين
        sess_id = _create_session(
            orig_chat, pending["study"], pending["break"],
            pending["creator_id"], pending["creator_name"], "",
            session_num=next_num,
            extra_participants=pending["participants"],
        )
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        text, keyboard = build_session_message(orig_chat, sess_id)
        msg = await context.bot.send_message(orig_chat, text, reply_markup=keyboard, parse_mode="Markdown")
        _sessions[orig_chat][sess_id]["message_id"] = msg.message_id
        await query.answer(f"✅ السشن {next_ord} جاهز — اضغط بدء!")
        return

    # ── إلغاء سشن معين من قبل المشرف ──
    if data.startswith("sess_cancel_admin:"):
        parts = data.split(":")
        tgt_chat = int(parts[1])
        tgt_sess = int(parts[2])
        # التحقق من صلاحيات المشرف
        is_admin_user = await is_admin_by_id(context, tgt_chat, user.id)
        if not is_admin_user and user.id != OWNER_CHAT_ID:
            await query.answer("❌ هذا الزر للمشرفين فقط.", show_alert=True)
            return
        if tgt_chat not in _sessions or tgt_sess not in _sessions[tgt_chat]:
            await query.answer("❌ هذا السشن لم يعد موجوداً.", show_alert=True)
            return
        session = _sessions[tgt_chat][tgt_sess]
        task = session.get("task")
        if task and not task.done():
            task.cancel()
        _pending_next_session.pop((tgt_chat, tgt_sess), None)
        ordinal = _session_ordinal(session.get("session_num", 1))
        _db_delete_session(tgt_chat, tgt_sess)
        _sessions[tgt_chat].pop(tgt_sess, None)
        if not _sessions.get(tgt_chat):
            _sessions.pop(tgt_chat, None)
        await query.answer(f"✅ تم إلغاء السشن {ordinal}.")
        # تحديث رسالة قائمة السشنات النشطة
        await _send_active_sessions_message(tgt_chat, context, edit_message=query.message)
        return


async def do_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إنهاء السشن — قائد السشن أو المشرف فقط، ويلغي السشن المرد عليه فقط."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None

    group_sessions = _sessions.get(chat_id, {})
    if not group_sessions:
        await update.message.reply_text("⚠️ لا يوجد سشن نشط حالياً.")
        return

    # محاولة تحديد السشن المقصود عبر الرد على رسالته
    target_sess_id = None
    replied_msg_id = (
        update.message.reply_to_message.message_id
        if update.message.reply_to_message
        else None
    )
    if replied_msg_id:
        for sid, sess in group_sessions.items():
            if sess.get("message_id") == replied_msg_id:
                target_sess_id = sid
                break

    # لو ما رد على رسالة، أو ما طابق أي سشن
    if target_sess_id is None:
        if len(group_sessions) == 1:
            # سشن واحد فقط → نحدده تلقائياً
            target_sess_id = next(iter(group_sessions))
        else:
            # أكثر من سشن → اطلب منه يرد على رسالة السشن المراد إلغاؤه
            await update.message.reply_text(
                "⚠️ في أكثر من سشن نشط.\n"
                "رد على رسالة السشن اللي تبي تلغيه واكتب «انهاء سشن»."
            )
            return

    session = group_sessions[target_sess_id]

    # فحص الصلاحية: قائد هذا السشن تحديداً أو المشرف أو المالك
    is_creator = session.get("creator_id") == user_id
    user_is_admin = await is_admin(update, context)
    if not is_creator and not user_is_admin and user_id != OWNER_CHAT_ID:
        await update.message.reply_text("❌ فقط قائد السشن أو المشرف يقدر يلغي السشن.")
        return

    # إلغاء هذا السشن فقط
    task = session.get("task")
    if task and not task.done():
        task.cancel()
    _pending_next_session.pop((chat_id, target_sess_id), None)
    ordinal = _session_ordinal(session.get("session_num", 1))
    count = len(session["participants"])
    names = " | ".join(p["name"] for p in session["participants"]) or "لا أحد"
    _db_clear_group_sessions(chat_id)  # يحذف السشن من MongoDB
    _sessions[chat_id].pop(target_sess_id, None)
    if not _sessions[chat_id]:
        _sessions.pop(chat_id, None)
    await update.message.reply_text(
        f"🏁 *انتهى السشن {ordinal}!*\n\n"
        f"⏱ {session['study']}د دراسة / ☕ {session['break']}د استراحة\n"
        f"👥 {count} مشارك: {names}",
        parse_mode="Markdown",
    )


async def _send_active_sessions_message(chat_id: int, context, edit_message=None):
    """يبني ويرسل (أو يعدّل) رسالة السشنات النشطة مع أزرار الإلغاء للمشرفين."""
    group_sessions = _sessions.get(chat_id, {})
    if not group_sessions:
        text = "ℹ️ لا توجد سشنات نشطة في المجموعة حالياً."
        keyboard = None
    else:
        lines = []
        buttons = []
        for sess_id, sess in group_sessions.items():
            ordinal = _session_ordinal(sess.get("session_num", 1))
            creator = _html_module.escape(sess.get("creator_name", "؟"))
            phase = "⏳ انتظار" if sess.get("phase") == "waiting" else "🔴 جاري الدراسة"
            participants = sess.get("participants", [])
            names = " | ".join(_html_module.escape(p["name"]) for p in participants) or "لا أحد"
            lines.append(
                f"📌 <b>السشن {ordinal}</b> — {phase}\n"
                f"👤 المنظم: {creator}\n"
                f"⏱ {sess['study']}د دراسة / ☕ {sess['break']}د استراحة\n"
                f"👥 المشاركون ({len(participants)}): {names}"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"❌ إلغاء السشن {ordinal}",
                    callback_data=f"sess_cancel_admin:{chat_id}:{sess_id}"
                )
            ])
        text = "📋 <b>السشنات النشطة</b>\n\n" + "\n\n".join(lines)
        keyboard = InlineKeyboardMarkup(buttons)

    if edit_message:
        try:
            await edit_message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass
    else:
        await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")


async def do_active_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض السشنات النشطة مع أزرار الإلغاء — للمشرفين فقط."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ هذا الأمر يشتغل في المجموعات فقط.")
        return
    is_admin_user = await is_admin_by_id(context, chat.id, user.id)
    if not is_admin_user and user.id != OWNER_CHAT_ID:
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    await _send_active_sessions_message(chat.id, context)


async def do_leave_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يسمح للعضو بالانسحاب من السشن الذي هو مشارك فيه."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ هذا الأمر يشتغل في المجموعات فقط.")
        return
    chat_id = chat.id
    user_id = user.id
    group_sessions = _sessions.get(chat_id, {})
    if not group_sessions:
        await update.message.reply_text("⚠️ لا يوجد سشن نشط حالياً.")
        return
    # البحث عن السشن الذي يشارك فيه العضو
    found_sess_id = None
    for sess_id, sess in group_sessions.items():
        if any(p["id"] == user_id for p in sess.get("participants", [])):
            found_sess_id = sess_id
            break
    if found_sess_id is None:
        await update.message.reply_text("⚠️ أنت لست مشاركاً في أي سشن نشط حالياً.")
        return
    session = group_sessions[found_sess_id]
    # إذا كان هو المنشئ لا يمكنه الانسحاب، بل يجب عليه إنهاء السشن
    if session.get("creator_id") == user_id:
        await update.message.reply_text(
            "⚠️ أنت منظم هذا السشن، لا تقدر تنسحب منه.\n"
            "لإنهاء السشن اكتب: <b>انهاء سشن</b>",
            parse_mode="HTML"
        )
        return
    # إزالة العضو من قائمة المشاركين
    session["participants"] = [p for p in session["participants"] if p["id"] != user_id]
    _db_save_session(chat_id, found_sess_id)
    ordinal = _session_ordinal(session.get("session_num", 1))
    await update.message.reply_text(
        f"✅ {_html_module.escape(user.first_name)} انسحب من السشن {ordinal}.",
        parse_mode="HTML"
    )
    # تحديث رسالة السشن إذا كانت موجودة
    msg_id = session.get("message_id")
    if msg_id:
        try:
            text, keyboard = build_session_message(chat_id, found_sess_id)
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, reply_markup=keyboard, parse_mode="HTML"
            )
        except Exception:
            pass


async def do_stop_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إيقاف منع التسخيت النشط للعضو في المجموعة."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ هذا الأمر يشتغل في المجموعات فقط.")
        return
    chat_id = chat.id
    user_id = user.id
    is_admin_user = await is_admin_by_id(context, chat_id, user_id)
    if is_admin_user:
        # المشرف يقدر يوقف منع التسخيت لأي شخص في المجموعة
        focus_map = _focus_sessions.get(chat_id, {})
        if not focus_map:
            await update.message.reply_text("⚠️ لا يوجد منع تسخيت نشط في المجموعة.")
            return
        for uid, sess in list(focus_map.items()):
            task = sess.get("task")
            if task and not task.done():
                task.cancel()
        _focus_sessions.pop(chat_id, None)
        await update.message.reply_text("✅ تم إيقاف منع التسخيت لجميع الأعضاء في المجموعة.")
    else:
        # العضو يوقف منع التسخيت الخاص فيه فقط
        focus_map = _focus_sessions.get(chat_id, {})
        sess = focus_map.get(user_id)
        if not sess:
            await update.message.reply_text("⚠️ ما عندك منع تسخيت نشط.")
            return
        task = sess.get("task")
        if task and not task.done():
            task.cancel()
        focus_map.pop(user_id, None)
        if not focus_map:
            _focus_sessions.pop(chat_id, None)
        # رفع الكتم إذا كان مكتوماً
        if sess.get("muted"):
            try:
                await context.bot.restrict_chat_member(
                    chat_id, user_id,
                    ChatPermissions(
                        can_send_messages=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                    ),
                )
            except Exception:
                pass
        await update.message.reply_text(f"✅ تم إيقاف منع التسخيت الخاص بك يا {user.first_name}.")


async def do_add_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء إضافة رد تلقائي — للمشرفين فقط."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    _pending_auto_reply[user_id] = {"step": "keyword", "chat_id": chat_id, "keyword": None}
    await update.message.reply_text("✏️ اكتب الكلمة المفتاحية الآن:")


async def do_delete_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف رد تلقائي — للمشرفين فقط."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    text = update.message.text.strip()
    keyword = None
    for prefix in ("حذف رد ", "حذف رد"):
        if text.startswith(prefix):
            keyword = text[len(prefix):].strip()
            break
    if not keyword:
        await update.message.reply_text("❗ اكتب الكلمة بعد الأمر، مثال:\nحذف رد [الكلمة]")
        return
    chat_id = update.effective_chat.id
    replies = _auto_replies.get(chat_id, {})
    if keyword.lower() in replies:
        del replies[keyword.lower()]
        _auto_replies[chat_id] = replies
        await update.message.reply_text(f"🗑 تم حذف الرد التلقائي للكلمة «{keyword}».")
    else:
        await update.message.reply_text(f"⚠️ ما في رد تلقائي للكلمة «{keyword}».")


async def do_list_auto_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة الردود التلقائية — للمشرفين فقط."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    replies = _auto_replies.get(chat_id, {})
    if not replies:
        await update.message.reply_text("📭 لا توجد ردود تلقائية مضافة.")
        return
    lines = ["📋 *الردود التلقائية:*\n"]
    for i, (kw, rep) in enumerate(replies.items(), 1):
        preview = rep[:60] + ("..." if len(rep) > 60 else "")
        lines.append(f"{i}. «{kw}» ← {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _get_sorted_rows(stats: dict, period: str) -> list:
    """يُرجع قائمة مرتبة بالمشاركين حسب الفترة."""
    now = datetime.now()
    if period == "today":
        cutoff = now.strftime("%Y-%m-%d")
    elif period == "week":
        cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "month":
        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        cutoff = None

    rows = []
    for uid, u in stats.items():
        if cutoff is None:
            sessions = u.get("sessions", 0)
            mins = u.get("study_minutes", 0)
        else:
            log = u.get("log", [])
            if period == "today":
                filtered = [e for e in log if e.get("date", "") == cutoff]
            else:
                filtered = [e for e in log if e.get("date", "") >= cutoff]
            sessions = len(filtered)
            mins = sum(e.get("study_minutes", 0) for e in filtered)
        if sessions == 0 and mins == 0:
            continue
        rows.append({"uid": uid, "name": u.get("name", "؟"), "sessions": sessions, "study_minutes": mins})

    rows.sort(key=lambda x: x["study_minutes"], reverse=True)
    return rows[:10]


def _period_label(period: str) -> str:
    return {
        "all":   "📊 الإحصائيات الكلية",
        "today": "📅 آخر يوم",
        "week":  "📆 آخر أسبوع",
        "month": "🗓 آخر شهر",
    }.get(period, "📊 الإحصائيات")


def _build_top10_keyboard(stats: dict, chat_id: int, period: str, user_id: int) -> tuple:
    """يبني رسالة + أزرار عمودية لأفضل 10 مشاركين."""
    rows = _get_sorted_rows(stats, period)
    label = _period_label(period)

    if not rows:
        text = f"{label}\n\n⚠️ لا توجد إحصائيات لهذه الفترة."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"statsperiod:{chat_id}:{user_id}:back")]
        ])
        return text, keyboard

    medals = ["🥇", "🥈", "🥉"]
    buttons = []
    for i, row in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        btn_label = f"{prefix}  {row['name']}"
        buttons.append([
            InlineKeyboardButton(btn_label, callback_data=f"statsuser:{chat_id}:{user_id}:{row['uid']}:{period}")
        ])
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"statsperiod:{chat_id}:{user_id}:back")])

    text = f"<b>{label}</b>\n\nاضغط على اسم لعرض إحصائياته التفصيلية 👇"
    return text, InlineKeyboardMarkup(buttons)


def _build_period_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    """يبني لوحة اختيار الفترة الزمنية."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 الإحصائيات الكلية", callback_data=f"statsperiod:{chat_id}:{user_id}:all")],
        [
            InlineKeyboardButton("📅 آخر يوم",   callback_data=f"statsperiod:{chat_id}:{user_id}:today"),
            InlineKeyboardButton("📆 آخر أسبوع", callback_data=f"statsperiod:{chat_id}:{user_id}:week"),
        ],
        [InlineKeyboardButton("🗓 آخر شهر",      callback_data=f"statsperiod:{chat_id}:{user_id}:month")],
    ])


async def show_group_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض قائمة اختيار فترة الإحصائيات."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ هذا الأمر يعمل في المجموعات فقط.")
        return
    chat_id = chat.id
    user_id = update.effective_user.id
    await update.message.reply_text(
        "📊 <b>إحصائيات الدراسة</b>\n\nاختر الفترة الزمنية:",
        parse_mode="HTML",
        reply_markup=_build_period_keyboard(chat_id, user_id),
    )


# يُبقى للتوافق مع أي استخدام داخلي آخر
def _build_stats_text(stats: dict, period: str) -> str:
    rows = _get_sorted_rows(stats, period)
    label = _period_label(period)
    if not rows:
        return f"{label}\n\n⚠️ لا توجد إحصائيات لهذه الفترة."
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = [f"<b>{label}</b>", ""]
    for i, row in enumerate(rows):
        mins = row["study_minutes"]
        hours = mins // 60
        rem = mins % 60
        time_str = f"{hours}س {rem}د" if hours else f"{rem}د"
        lines.append(f"{medals[i]} {_html_module.escape(row['name'])} — {row['sessions']} جلسة | ⏱ {time_str}")
    return "\n".join(lines)


async def handle_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أزرار الإحصائيات: اختيار فترة، قائمة مشاركين، تفاصيل مستخدم."""
    query = update.callback_query
    data = query.data
    presser_id = query.from_user.id

    # ── اختيار الفترة: statsperiod:{chat_id}:{user_id}:{period|back} ──
    if data.startswith("statsperiod:"):
        parts = data.split(":")
        chat_id = int(parts[1])
        owner_id = int(parts[2])
        period = parts[3]
        if presser_id != owner_id:
            await query.answer("❌ هذه الأزرار خاصة بمن طلب الأمر فقط.", show_alert=True)
            return
        await query.answer()
        stats = _session_stats.get(chat_id, {})
        if period == "back":
            try:
                await query.edit_message_text(
                    "📊 <b>إحصائيات الدراسة</b>\n\nاختر الفترة الزمنية:",
                    parse_mode="HTML",
                    reply_markup=_build_period_keyboard(chat_id, owner_id),
                )
            except Exception:
                pass
            return
        text, keyboard = _build_top10_keyboard(stats, chat_id, period, owner_id)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass
        return

    # ── تفاصيل مستخدم: statsuser:{chat_id}:{user_id}:{uid}:{period} ──
    if data.startswith("statsuser:"):
        parts = data.split(":")
        chat_id = int(parts[1])
        owner_id = int(parts[2])
        uid = int(parts[3])
        period = parts[4]
        if presser_id != owner_id:
            await query.answer("❌ هذه الأزرار خاصة بمن طلب الأمر فقط.", show_alert=True)
            return
        await query.answer()
        stats = _session_stats.get(chat_id, {})
        u = stats.get(uid)
        if not u:
            await query.answer("❌ لا توجد إحصائيات لهذا المستخدم.", show_alert=True)
            return
        rows = _get_sorted_rows(stats, period)
        user_row = next((r for r in rows if r["uid"] == uid), None)
        all_rows = _get_sorted_rows(stats, "all")
        rank_all = next((i + 1 for i, r in enumerate(all_rows) if r["uid"] == uid), "-")
        if user_row:
            sessions = user_row["sessions"]
            mins = user_row["study_minutes"]
        else:
            sessions, mins = 0, 0
        hours = mins // 60
        rem = mins % 60
        time_str = f"{hours} ساعة و{rem} دقيقة" if hours else f"{mins} دقيقة"
        label = _period_label(period)
        name = _html_module.escape(u.get("name", "؟"))
        medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
        rank_period = next((i + 1 for i, r in enumerate(rows) if r["uid"] == uid), "-")
        medal = medals[rank_period - 1] if isinstance(rank_period, int) and rank_period <= 10 else "👤"
        text = (
            f"{medal} <b>{name}</b>\n"
            f"──────────────────\n"
            f"📅 الفترة: <b>{label}</b>\n"
            f"📚 الجلسات: <b>{sessions}</b>\n"
            f"⏱ وقت الدراسة: <b>{time_str}</b>\n"
            f"🏆 الترتيب في الفترة: <b>#{rank_period}</b>\n"
            f"🌟 الترتيب العام: <b>#{rank_all}</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data=f"statsperiod:{chat_id}:{owner_id}:{period}")]
        ])
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass
        return


async def show_my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض إحصائيات المستخدم الشخصية في هذه المجموعة."""
    chat_id = update.effective_chat.id
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ هذا الأمر يعمل في المجموعات فقط.")
        return
    if not user:
        return
    stats = _session_stats.get(chat_id, {})
    u = stats.get(user.id)
    if not u or u["sessions"] == 0:
        await update.message.reply_text(
            f"📊 <b>إحصائياتك في هذه المجموعة</b>\n\n"
            f"لا توجد إحصائيات بعد.\nابدأ سشناً واضغط <b>انا موجود</b> عند انتهاء الدراسة.",
            parse_mode="HTML"
        )
        return
    sessions = u["sessions"]
    mins = u["study_minutes"]
    hours = mins // 60
    rem_mins = mins % 60
    time_str = f"{hours} ساعة و{rem_mins} دقيقة" if hours else f"{mins} دقيقة"
    # ترتيب المستخدم بين الأعضاء
    sorted_ids = sorted(stats, key=lambda uid: stats[uid]["study_minutes"], reverse=True)
    rank = sorted_ids.index(user.id) + 1 if user.id in sorted_ids else "-"
    name = _html_module.escape(user.first_name)
    await update.message.reply_text(
        f"📊 <b>إحصائيات {name}</b>\n\n"
        f"📚 جلسات الدراسة: <b>{sessions}</b>\n"
        f"⏱ إجمالي وقت الدراسة: <b>{time_str}</b>\n"
        f"🏆 ترتيبك في المجموعة: <b>#{rank}</b>",
        parse_mode="HTML"
    )


# ============================================================
# 👋 دوال الترحيب بالأعضاء الجدد
# ============================================================


# ============================================================
# 🛡 حماية مالك البوت — استعادة تلقائية عند الكتم أو الحظر
# ============================================================

async def handle_owner_protection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يراقب تغيّر حالة مالك البوت ويزيل أي قيود عليه تلقائياً."""
    chat_member = update.chat_member
    if not chat_member:
        return

    user = chat_member.new_chat_member.user
    if user.id != OWNER_CHAT_ID:
        return

    chat_id = chat_member.chat.id
    new_status = chat_member.new_chat_member.status

    if new_status == "kicked" or new_status == "banned":
        try:
            await context.bot.unban_chat_member(chat_id, OWNER_CHAT_ID)
        except Exception:
            pass

    elif new_status == "restricted":
        member = chat_member.new_chat_member
        can_send = getattr(member, "can_send_messages", True)
        if not can_send:
            try:
                await context.bot.restrict_chat_member(
                    chat_id,
                    OWNER_CHAT_ID,
                    ChatPermissions(
                        can_send_messages=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                    ),
                )
            except Exception:
                pass


async def do_enable_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    if _welcome_enabled.get(chat_id, True) is True:
        await update.message.reply_text("ℹ️ رسائل الترحيب مفعّلة مسبقاً.")
        return
    _welcome_enabled[chat_id] = True
    save_data()
    await update.message.reply_text("✅ تم تفعيل رسائل الترحيب بالأعضاء الجدد.")


async def do_disable_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    if _welcome_enabled.get(chat_id, True) is False:
        await update.message.reply_text("ℹ️ رسائل الترحيب معطّلة مسبقاً.")
        return
    _welcome_enabled[chat_id] = False
    save_data()
    await update.message.reply_text("❌ تم تعطيل رسائل الترحيب.")


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يرحب بالأعضاء الجدد إذا كان الترحيب مفعّلاً."""
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not _welcome_enabled.get(chat_id, True):
        return
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        name = member.first_name or "عضو جديد"
        msg = random.choice(WELCOME_MESSAGES).format(name=name)
        try:
            await update.message.reply_text(msg)
        except Exception:
            pass


# ============================================================
# 🚫 دوال تقييد الوسائط
# ============================================================

def _is_vip(chat_id: int, user_id: int) -> bool:
    """يتحقق إن كان العضو مميزاً في المجموعة."""
    return user_id in _vip_users.get(chat_id, set())


async def _check_media_restriction(
    update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str
) -> bool:
    """يحذف الوسائط المقيّدة ويعيد True إذا تم الحذف."""
    if not update.message or not update.effective_chat:
        return False
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return False
    chat_id = chat.id
    user_id = update.effective_user.id if update.effective_user else None
    restricted = _media_restrictions.get(chat_id, set())
    if media_type not in restricted:
        return False
    # المميزون والمشرفون يتجاوزون القيود
    if user_id and _is_vip(chat_id, user_id):
        return False
    if user_id and await is_admin_by_id(context, chat_id, user_id):
        return False
    try:
        await update.message.delete()
    except Exception:
        pass
    name = update.effective_user.first_name if update.effective_user else "العضو"
    type_names = {
        "photo": "الصور", "video": "الفيديوهات", "document": "الملفات",
        "sticker": "الستيكرات", "animation": "الصور المتحركة",
        "voice": "الرسائل الصوتية", "audio": "الموسيقى",
    }
    try:
        await context.bot.send_message(
            chat_id,
            f"🚫 {name}، إرسال {type_names.get(media_type, 'هذا النوع')} غير مسموح في هذه المجموعة."
        )
    except Exception:
        pass
    return True


async def media_handler_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _check_media_restriction(update, context, "video")


async def media_handler_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _check_media_restriction(update, context, "document")


async def media_handler_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _check_media_restriction(update, context, "sticker")


async def media_handler_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _check_media_restriction(update, context, "animation")


async def media_handler_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _check_media_restriction(update, context, "voice")


async def media_handler_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _check_media_restriction(update, context, "audio")


_MEDIA_TYPE_NAMES = {
    "photo": "الصور", "video": "الفيديوهات", "document": "الملفات",
    "sticker": "الستيكرات", "animation": "الصور المتحركة",
    "voice": "الرسائل الصوتية", "audio": "الموسيقى",
}


def _make_restrict_cmd(media_type: str):
    async def _restrict(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
            return
        chat_id = update.effective_chat.id
        restricted = _media_restrictions.get(chat_id, set())
        label = _MEDIA_TYPE_NAMES.get(media_type, media_type)
        if media_type in restricted:
            await update.message.reply_text(f"ℹ️ {label} معطّلة مسبقاً في هذه المجموعة.")
            return
        if chat_id not in _media_restrictions:
            _media_restrictions[chat_id] = set()
        _media_restrictions[chat_id].add(media_type)
        save_data()
        await update.message.reply_text(
            f"🚫 تم تعطيل {label} في المجموعة.\n"
            f"_الأعضاء المميزون والمشرفون مستثنون._"
        )
    return _restrict


def _make_allow_cmd(media_type: str):
    async def _allow(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
            return
        chat_id = update.effective_chat.id
        restricted = _media_restrictions.get(chat_id, set())
        label = _MEDIA_TYPE_NAMES.get(media_type, media_type)
        if media_type not in restricted:
            await update.message.reply_text(f"ℹ️ {label} مسموح بها أصلاً في هذه المجموعة.")
            return
        _media_restrictions[chat_id].discard(media_type)
        save_data()
        await update.message.reply_text(f"✅ تم السماح بـ{label} في المجموعة.")
    return _allow


# إنشاء الدوال الـ 14 تلقائياً
do_restrict_photo = _make_restrict_cmd("photo")
do_allow_photo = _make_allow_cmd("photo")
do_restrict_video = _make_restrict_cmd("video")
do_allow_video = _make_allow_cmd("video")
do_restrict_document = _make_restrict_cmd("document")
do_allow_document = _make_allow_cmd("document")
do_restrict_sticker = _make_restrict_cmd("sticker")
do_allow_sticker = _make_allow_cmd("sticker")
do_restrict_voice = _make_restrict_cmd("voice")
do_allow_voice = _make_allow_cmd("voice")
do_restrict_audio = _make_restrict_cmd("audio")
do_allow_audio = _make_allow_cmd("audio")
do_restrict_animation = _make_restrict_cmd("animation")
do_allow_animation = _make_allow_cmd("animation")


# ============================================================
# ⭐ دوال الأعضاء المميزين
# ============================================================

async def do_vip_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رفع عضو إلى مميز — يتجاوز قيود الوسائط."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    target = await get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ رد على رسالة العضو اللي تبيه مميزاً.")
        return
    if target.id in _vip_users.get(chat_id, set()):
        await update.message.reply_text(f"ℹ️ *{target.first_name}* مميز مسبقاً.", parse_mode="Markdown")
        return
    if chat_id not in _vip_users:
        _vip_users[chat_id] = set()
    _vip_users[chat_id].add(target.id)
    save_data()
    await update.message.reply_text(
        f"⭐ تم رفع {target.first_name} إلى عضو مميز.\n"
        f"_يستطيع الآن إرسال جميع أنواع الوسائط حتى لو كانت مقيّدة._"
    )


async def do_vip_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنزيل عضو من المميزين."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    target = await get_target_user(update)
    if not target:
        await update.message.reply_text("⚠️ رد على رسالة العضو اللي تبي تنزّله من المميزين.")
        return
    if target.id not in _vip_users.get(chat_id, set()):
        await update.message.reply_text(f"ℹ️ *{target.first_name}* مو مميز أصلاً.", parse_mode="Markdown")
        return
    _vip_users[chat_id].discard(target.id)
    save_data()
    await update.message.reply_text(f"✅ تم تنزيل {target.first_name} من قائمة المميزين.")


async def do_vip_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة الأعضاء المميزين في المجموعة."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    vips = _vip_users.get(chat_id, set())
    if not vips:
        await update.message.reply_text("⭐ لا يوجد أعضاء مميزون في هذه المجموعة حالياً.")
        return
    lines = [f"⭐ *الأعضاء المميزون في المجموعة:*\n"]
    for uid in vips:
        user_obj = _id_to_user.get(uid)
        name = user_obj.first_name if user_obj else str(uid)
        username = f" (@{user_obj.username})" if user_obj and user_obj.username else ""
        lines.append(f"• {name}{username}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ============================================================
# 🔴🟢 تعطيل / تفعيل جميع الوسائط دفعة واحدة
# ============================================================

_ALL_MEDIA_TYPES = {"photo", "video", "document", "sticker", "animation", "voice", "audio"}


async def do_restrict_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تقييد جميع أنواع الوسائط في المجموعة دفعة واحدة."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    current = _media_restrictions.get(chat_id, set())
    if current == _ALL_MEDIA_TYPES:
        await update.message.reply_text("ℹ️ جميع الوسائط معطّلة مسبقاً في هذه المجموعة.")
        return
    _media_restrictions[chat_id] = set(_ALL_MEDIA_TYPES)
    save_data()
    await update.message.reply_text(
        "🚫 *تم تعطيل جميع الوسائط في المجموعة*\n\n"
        "المحظورة: الصور، الفيديو، الملفات، الستيكر، الجيف، الصوت، الموسيقى\n\n"
        "_المشرفون والأعضاء المميزون مستثنون تلقائياً_",
        parse_mode="Markdown"
    )


async def do_allow_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رفع جميع تقييدات الوسائط في المجموعة دفعة واحدة."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر للمشرفين فقط.")
        return
    chat_id = update.effective_chat.id
    if not _media_restrictions.get(chat_id):
        await update.message.reply_text("ℹ️ ما في أي قيود على الوسائط في هذه المجموعة أصلاً.")
        return
    _media_restrictions[chat_id] = set()
    save_data()
    await update.message.reply_text(
        "✅ *تم السماح بجميع الوسائط في المجموعة*\n\n"
        "لا توجد أي قيود على الوسائط حالياً.",
        parse_mode="Markdown"
    )


COMMAND_HANDLERS = {
    "ban": do_ban,
    "unban": do_unban,
    "mute": do_mute,
    "unmute": do_unmute,
    "kick": do_kick,
    "warn": do_warn,
    "pin": do_pin,
    "unpin": do_unpin,
    "delete": do_delete,
    "info": do_info,
    "promote": do_promote,
    "demote": do_demote,
    "add_reply": do_add_auto_reply,
    "delete_reply": do_delete_auto_reply,
    "list_replies": do_list_auto_replies,
    "end_session": do_end_session,
    "leave_session": do_leave_session,
    "active_sessions": do_active_sessions,
    "stop_focus": do_stop_focus,
    "help": show_help,
    "rate_limit": do_rate_limit,
    "cancel_rate_limit": do_cancel_rate_limit,
    "stats": show_group_stats,
    "my_stats": show_my_stats,
    # الترحيب
    "enable_welcome": do_enable_welcome,
    "disable_welcome": do_disable_welcome,
    # تقييد الوسائط
    "restrict_photo": do_restrict_photo,
    "allow_photo": do_allow_photo,
    "restrict_video": do_restrict_video,
    "allow_video": do_allow_video,
    "restrict_document": do_restrict_document,
    "allow_document": do_allow_document,
    "restrict_sticker": do_restrict_sticker,
    "allow_sticker": do_allow_sticker,
    "restrict_voice": do_restrict_voice,
    "allow_voice": do_allow_voice,
    "restrict_audio": do_restrict_audio,
    "allow_audio": do_allow_audio,
    "restrict_animation": do_restrict_animation,
    "allow_animation": do_allow_animation,
    # المميزون
    "vip_add": do_vip_add,
    "vip_remove": do_vip_remove,
    "vip_list": do_vip_list,
    # الكل دفعة واحدة
    "restrict_all": do_restrict_all,
    "allow_all": do_allow_all,
}


YOUTUBE_PLAY_KEYWORDS = ["شغل", "شغّل", "شغلي", "ابحث عن", "بحث عن", "play"]


def detect_youtube_request(text: str):
    text_lower = text.strip().lower()
    for kw in YOUTUBE_PLAY_KEYWORDS:
        for trigger in BOT_TRIGGER_WORDS:
            patterns = [
                f"{trigger.lower()} {kw} ",
                f"{trigger.lower()} {kw}\n",
                f"{kw} ",
            ]
            for pat in patterns[:2]:
                if text_lower.startswith(pat):
                    query = text[len(pat):].strip()
                    if query:
                        return query
            if text_lower.startswith(kw + " "):
                for t in BOT_TRIGGER_WORDS:
                    if t.lower() in text_lower:
                        query = text_lower.replace(kw, "").strip()
                        for t2 in BOT_TRIGGER_WORDS:
                            query = query.replace(t2.lower(), "").strip()
                        if query:
                            return query
    return None


def download_youtube_video(query: str):
    try:
        results = Search(query).videos
        if not results:
            return None
    except Exception as e:
        logger.warning(f"فشل البحث في يوتيوب: {e}")
        return None

    for video in results[:5]:
        tmp_dir = tempfile.mkdtemp()
        try:
            yt = YouTube(video.watch_url)
            stream = (
                yt.streams.filter(progressive=True, file_extension="mp4")
                .order_by("resolution")
                .desc()
                .first()
            )
            if not stream:
                stream = yt.streams.filter(file_extension="mp4").order_by("resolution").desc().first()
            if not stream:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue
            file_path = stream.download(output_path=tmp_dir, filename="video.mp4")
            if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                return file_path
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"فشل تحميل {video.watch_url}: {e}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            continue
    return None


def search_youtube_titles(query: str):
    search_clients = [["tv_embedded"], ["ios"], ["web_embedded"], ["android"], ["mweb"]]
    for client in search_clients:
        try:
            ydl_opts = {
                **_YT_BASE_OPTS,
                "extract_flat": True,
                "extractor_args": {"youtube": {"player_client": client}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(f"ytsearch5:{query}", download=False)
                if result and "entries" in result:
                    entries = [
                        (e["title"], f"https://www.youtube.com/watch?v={e['id']}")
                        for e in result["entries"]
                        if e.get("title") and e.get("id")
                    ]
                    if entries:
                        return entries
        except Exception as e:
            logger.warning(f"فشل البحث ({client}): {e}")
            continue
    return []


async def bot_youtube_response(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    user = update.effective_user
    first_name = user.first_name or "أخي"
    msg = await update.message.reply_text("🔍 أبحث...")
    results = await asyncio.get_running_loop().run_in_executor(None, search_youtube_titles, query)
    if not results:
        await msg.edit_text(f"{first_name}، ما لقيت نتائج لـ \"{query}\".")
        return

    context.bot_data[f"yt_{user.id}"] = results

    buttons = [
        [InlineKeyboardButton(f"{i+1}. {title[:55]}", callback_data=f"yt_pick_{user.id}_{i}")]
        for i, (title, _) in enumerate(results)
    ]
    await msg.edit_text("اختر الفيديو:", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_yt_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    user_id = int(parts[2])
    idx = int(parts[3])

    if query.from_user.id != user_id:
        await query.answer("هذا الاختيار مو إلك.", show_alert=True)
        return

    results = context.bot_data.get(f"yt_{user_id}")
    if not results or idx >= len(results):
        await query.edit_message_text("انتهت صلاحية النتائج، جرب من جديد.")
        return

    title, url = results[idx]
    context.bot_data[f"yt_choice_{user_id}"] = (title, url)

    buttons = [
        [
            InlineKeyboardButton("🎬 فيديو", callback_data=f"yt_fmt_{user_id}_video"),
            InlineKeyboardButton("🎵 صوت فقط", callback_data=f"yt_fmt_{user_id}_audio"),
        ]
    ]
    await query.edit_message_text(
        f"اخترت: {title}\n\nكيف تريده؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _clear_dir(tmp_dir: str):
    for f in os.listdir(tmp_dir):
        try:
            os.remove(os.path.join(tmp_dir, f))
        except Exception:
            pass


def _find_file(tmp_dir: str):
    for f in os.listdir(tmp_dir):
        full = os.path.join(tmp_dir, f)
        if os.path.isfile(full) and os.path.getsize(full) > 0:
            return full
    return None


# الـ clients مرتبة من الأنجح على السيرفرات للأقل نجاحاً
_YT_CLIENTS = [
    ["tv_embedded"],
    ["ios"],
    ["web_embedded"],
    ["android_vr"],
    ["mweb"],
    ["android"],
    ["web"],
]

# خيارات مشتركة تساعد على تجاوز حجب السيرفر
_YT_COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt_cookies.txt")

_YT_BASE_OPTS = {
    "quiet": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "geo_bypass": True,
    "socket_timeout": 45,
    **({"cookiefile": _YT_COOKIES_FILE} if os.path.isfile(_YT_COOKIES_FILE) else {}),
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.youtube.com/",
    },
}


def download_video_file(url: str):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "video.%(ext)s")
    for client in _YT_CLIENTS:
        ydl_opts = {
            **_YT_BASE_OPTS,
            "outtmpl": output_path,
            "format": "18/best[ext=mp4][filesize<45M]/best[height<=720][filesize<45M]/best[filesize<45M]",
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": client}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            result = _find_file(tmp_dir)
            if result:
                logger.info(f"نجح تحميل الفيديو بـ {client}")
                return result
        except Exception as e:
            logger.warning(f"فشل تحميل فيديو ({client}): {e}")
            _clear_dir(tmp_dir)
            continue
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def download_audio_file(url: str):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "audio.%(ext)s")
    for client in _YT_CLIENTS:
        ydl_opts = {
            **_YT_BASE_OPTS,
            "outtmpl": output_path,
            "format": "140/bestaudio[ext=m4a]/bestaudio[filesize<45M]/18",
            "max_filesize": 45 * 1024 * 1024,
            "extractor_args": {"youtube": {"player_client": client}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            result = _find_file(tmp_dir)
            if result:
                logger.info(f"نجح تحميل الصوت بـ {client}")
                return result
        except Exception as e:
            logger.warning(f"فشل تحميل صوت ({client}): {e}")
            _clear_dir(tmp_dir)
            continue
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


async def handle_yt_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    user_id = int(parts[2])
    fmt = parts[3]

    if query.from_user.id != user_id:
        await query.answer("هذا الاختيار مو إلك.", show_alert=True)
        return

    choice = context.bot_data.get(f"yt_choice_{user_id}")
    if not choice:
        await query.edit_message_text("انتهت صلاحية الاختيار، جرب من جديد.")
        return

    title, url = choice
    await query.edit_message_text("⏳ جاري التحميل...")

    if fmt == "video":
        file_path = await asyncio.get_running_loop().run_in_executor(None, download_video_file, url)
        if file_path:
            with open(file_path, "rb") as f:
                await query.message.reply_video(video=f, supports_streaming=True)
            await query.message.delete()
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        else:
            await query.edit_message_text("ما قدرت أحمّل الفيديو، جرب مرة ثانية.")
    else:
        file_path = await asyncio.get_running_loop().run_in_executor(None, download_audio_file, url)
        if file_path:
            with open(file_path, "rb") as f:
                await query.message.reply_audio(audio=f, title=title)
            await query.message.delete()
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        else:
            await query.edit_message_text("ما قدرت أحمّل الصوت، جرب مرة ثانية.")


def _get_user_history(user_id: int) -> list:
    """يعيد تاريخ المحادثة النظيف (يحذف الرسائل المنتهية الصلاحية)."""
    import time
    if not _history_enabled:
        return []
    now = time.time()
    expiry = _history_expiry_minutes * 60
    entries = _user_history.get(user_id, [])
    fresh = [e for e in entries if now - e["ts"] < expiry]
    _user_history[user_id] = fresh
    return fresh


def _add_to_history(user_id: int, user_text: str, model_text: str):
    """يضيف رسالة المستخدم ورد البوت إلى التاريخ مع تطبيق الـ sliding window."""
    import time
    if not _history_enabled:
        return
    now = time.time()
    entries = _user_history.get(user_id, [])
    entries.append({"role": "user",  "text": user_text,  "ts": now})
    entries.append({"role": "model", "text": model_text, "ts": now})
    # احتفظ فقط بآخر (max_messages * 2) إدخال (كل زوج = رسالة مستخدم + رد بوت)
    max_entries = _history_max_messages * 2
    _user_history[user_id] = entries[-max_entries:]


def _build_contents_with_history(user_id: int, current_message: str) -> list:
    """يبني قائمة المحتويات للـ Gemini مع التاريخ."""
    import time
    history = _get_user_history(user_id)
    if not history:
        return current_message
    contents = []
    for entry in history:
        contents.append(
            types.Content(
                role=entry["role"],
                parts=[types.Part.from_text(text=entry["text"])],
            )
        )
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=current_message)],
        )
    )
    return contents


async def bot_call_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name or "أخي"
    user_id = user.id
    user_message = update.message.text.strip()
    now = datetime.now()
    chat = update.effective_chat

    # ── التحقق من حدود الذكاء ──
    if chat and chat.type != "private":
        chat_id_check = chat.id
        ok, reason = is_ai_allowed_for_chat(chat_id_check)
        if not ok:
            if reason == "disabled":
                await update.message.reply_text("🤖 الذكاء الاصطناعي معطّل في هذه المجموعة حالياً.")
            else:
                limit = _ai_daily_limit.get(chat_id_check, 0)
                await update.message.reply_text(
                    f"⏰ تم استنفاد الحد اليومي للذكاء ({limit} طلب) في هذه المجموعة.\n"
                    "_سيُعاد الحد تلقائياً غداً._",
                    parse_mode="Markdown",
                )
            return

    if user_id in _ignored_users:
        if now < _ignored_users[user_id]:
            if is_asking_forgiveness(user_message):
                if user_id not in _forgiven_users:
                    _forgiven_users.add(user_id)
                    del _ignored_users[user_id]
                    if user_id in _warned_users:
                        del _warned_users[user_id]
                    await update.message.reply_text(
                        f"حسناً {first_name}، سامحتك — بس هاذي المرة بس. "
                        f"إذا كررت الأسلوب ما راح أسامح مرة ثانية."
                    )
                else:
                    await update.message.reply_text(
                        f"{first_name}، سبق وسامحتك مرة، ما راح أسامح مرة ثانية. "
                        f"انتظر انتهاء الساعة."
                    )
            return
        else:
            del _ignored_users[user_id]

    if user_id in _warned_users:
        warning_time = _warned_users[user_id]
        if now - warning_time > timedelta(minutes=WARNING_EXPIRY_MINUTES):
            del _warned_users[user_id]

    # ── لو الرسالة مجرد اسم البوت أو رمز قصير — رد نداء مباشر ──
    _stripped_call = user_message.strip().lower()
    for _tr in BOT_TRIGGER_WORDS:
        _stripped_call = _stripped_call.replace(_tr.lower(), "")
    _stripped_call = _stripped_call.strip("،.؟?!_ \t\n")
    if len(_stripped_call) <= 2:
        # أولوية لرد المشرفين المخصص: نبحث في الردود التلقائية عن أي كلمة تشغيل
        _chat_id_call = update.effective_chat.id if update.effective_chat else None
        _admin_reply_for_name = None
        if _chat_id_call and _chat_id_call in _auto_replies:
            _call_text_lower = user_message.strip().lower()
            for _kw, _kw_reply in _auto_replies[_chat_id_call].items():
                if _is_whole_word(_kw, _call_text_lower):
                    _admin_reply_for_name = _kw_reply
                    break
        if _admin_reply_for_name:
            await update.message.reply_text(_admin_reply_for_name)
        else:
            reply = random.choice(BOT_RESPONSES)
            await update.message.reply_text(reply)
        return

    # ── الردود المحفوظة تأتي أولاً (أسرع ولا تستهلك API) ──
    reply = get_smart_fallback(first_name, user_message)

    if not reply:
        contents = _build_contents_with_history(user_id, user_message)
        ai_error = None
        try:
            _call_chat_id = update.effective_chat.id if update.effective_chat else None
            response = generate_with_rotation_for_group(
                chat_id=_call_chat_id,
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=GEMINI_SYSTEM_PROMPT,
                    max_output_tokens=8192,
                ),
            )
            reply = _clean_ai_reply(response.text) if response.text else None
            if not reply:
                raise ValueError("رد فارغ من الذكاء الاصطناعي")
        except Exception as e:
            err_str = str(e)
            logger.error(f"Gemini فشل [{type(e).__name__}]: {err_str}")
            ai_error = err_str
            reply = None

    if not reply:
        if ai_error is not None:
            # إرسال تفاصيل الخطأ للمالك
            if _bot_app and OWNER_CHAT_ID:
                try:
                    short_err = ai_error[:300]
                    chat_info = f"المجموعة: {update.message.chat.title or 'خاص'} (ID: {update.message.chat_id})"
                    user_info = f"المستخدم: {update.message.from_user.full_name} (ID: {user_id})"
                    asyncio.create_task(
                        _bot_app.bot.send_message(
                            OWNER_CHAT_ID,
                            f"🚨 <b>خطأ في الذكاء الاصطناعي</b>\n{chat_info}\n{user_info}\n\n<code>{short_err}</code>",
                            parse_mode="HTML",
                        )
                    )
                except Exception:
                    pass
        return

    if "##RUDE##" in reply:
        if user_id in _warned_users:
            _ignored_users[user_id] = now + timedelta(hours=IGNORE_DURATION_HOURS)
            del _warned_users[user_id]
            await update.message.reply_text(
                f"{first_name}، حذّرتك مرة وما انتبهت. "
                f"راح أتجاهل رسائلك ساعة كاملة."
            )
        else:
            _warned_users[user_id] = now
            await update.message.reply_text(
                f"{first_name}، هذا أسلوب ما يصلح. "
                f"تكلم باحترام — هذا تحذير، إذا كررت راح أتجاهلك ساعة."
            )
        return

    # ── تسجيل الاستخدام اليومي للذكاء ──
    if chat and chat.type != "private":
        increment_ai_usage(chat.id)

    # ── حفظ التاريخ ──
    _add_to_history(user_id, user_message, reply)

    await update.message.reply_text(reply)


async def bot_photo_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name or "أخي"
    caption = update.message.caption or ""

    photo = update.message.photo[-1]
    photo_file = await context.bot.get_file(photo.file_id)
    photo_bytes = await photo_file.download_as_bytearray()

    prompt = "أرسل العضو هذه الصورة"
    if caption:
        prompt += f" وكتب: {caption}"
    prompt += ". إذا في الصورة سؤال أو مسألة دراسية أو أي استفسار، حلّه بشكل معتدل يوضح الفكرة الأساسية والخطوات الرئيسية بإيجاز والناتج النهائي، بدون شرح مطوّل أو تكرار. إذا ما في سؤال، وصف ما تشوفه بإيجاز."

    try:
        response = generate_with_rotation(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM_PROMPT,
                max_output_tokens=8192,
            ),
        )
        reply = _clean_ai_reply(response.text) if response.text else None
        if not reply:
            raise ValueError("رد فارغ من الذكاء الاصطناعي")
    except Exception as e:
        logger.warning(f"Gemini فشل مع الصورة: {e}")
        reply = "ما قدرت أقرأ الصورة، جرب ترسلها مرة ثانية."

    await update.message.reply_text(reply)


async def bot_reply_to_photo_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name or "أخي"
    user_request = update.message.text.strip()
    replied_msg = update.message.reply_to_message

    photo = replied_msg.photo[-1]
    photo_file = await context.bot.get_file(photo.file_id)
    photo_bytes = await photo_file.download_as_bytearray()

    original_sender = replied_msg.from_user.first_name if replied_msg.from_user else "شخص"
    original_caption = replied_msg.caption.strip() if replied_msg.caption else ""

    prompt = "رد العضو على صورة"
    if original_caption:
        prompt += f" كتب معها: \"{original_caption}\""
    prompt += f". طلب العضو: {user_request}. اقرأ الصورة وجاوب على الطلب بشكل معتدل يوضح الفكرة الأساسية والخطوات الرئيسية بإيجاز والناتج النهائي، بدون شرح مطوّل أو تكرار."

    try:
        response = generate_with_rotation(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM_PROMPT,
                max_output_tokens=8192,
            ),
        )
        reply = _clean_ai_reply(response.text) if response.text else None
        if not reply:
            raise ValueError("رد فارغ من الذكاء الاصطناعي")
    except Exception as e:
        logger.warning(f"Gemini فشل مع صورة الرد: {e}")
        reply = "ما قدرت أقرأ الصورة، جرب مرة ثانية."

    await update.message.reply_text(reply)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    # فحص تقييد الصور أولاً
    if await _check_media_restriction(update, context, "photo"):
        return

    caption = update.message.caption or ""

    if is_calling_bot(caption):
        await bot_photo_response(update, context)
        return

    if (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    ):
        await bot_photo_response(update, context)
        return


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _owner_username
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id if update.effective_user else None
    chat = update.effective_chat

    # ── تتبع معلومات المالك ──
    if user_id == OWNER_CHAT_ID:
        if update.effective_user and update.effective_user.username and not _owner_username:
            _owner_username = update.effective_user.username

    # ── تتبع المجموعات وإشعار المالك عند الاكتشاف الأول ──
    if chat and chat.type in ("group", "supergroup"):
        is_new_group = chat.id not in _owner_known_chats
        _owner_known_chats.add(chat.id)
        if chat.title:
            _known_chat_names[chat.id] = chat.title
        if chat.username:
            _known_chat_usernames[chat.id] = chat.username
        if is_new_group:
            link_text = f"\n🔗 الرابط: https://t.me/{chat.username}" if chat.username else ""
            try:
                await context.bot.send_message(
                    OWNER_CHAT_ID,
                    f"🆕 *البوت نشط في مجموعة جديدة!*\n\n"
                    f"📍 الاسم: {chat.title or 'غير معروف'}\n"
                    f"🆔 المعرّف: `{chat.id}`"
                    f"{link_text}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            save_data()

    # ── كاش بيانات المستخدمين (لدعم البحث بـ @يوزرنيم) ──
    if update.effective_user:
        _u = update.effective_user
        _id_to_user[_u.id] = _u
        if _u.username:
            _username_to_id[_u.username.lower()] = _u.id

    # ============================================================
    # قيود المحادثات الخاصة — غير المالك وغير المشرفين
    # ============================================================
    if chat and chat.type == "private":
        if user_id != OWNER_CHAT_ID and user_id not in _bot_admins:
            bot_username = (await context.bot.get_me()).username
            await update.message.reply_text(
                "👋 *أهلاً بك!*\n\n"
                "لتفعيل البوت في مجموعتك اتبع هالخطوات:\n\n"
                f"1️⃣ افتح مجموعتك\n"
                f"2️⃣ اضغط على اسم المجموعة → *أعضاء* → *إضافة عضو*\n"
                f"3️⃣ ابحث عن `@{bot_username}` وأضفه\n"
                f"4️⃣ منح البوت صلاحية إرسال الرسائل\n\n"
                "✅ البوت يبدأ يشتغل تلقائياً بمجرد إضافته!",
                parse_mode="Markdown",
            )
            return

    # ============================================================
    # معالجة الإدخالات المعلّقة للإعدادات
    # ============================================================
    if user_id and user_id in _pending_settings_input:
        state = _pending_settings_input.pop(user_id)
        val = text.strip()

        if state["type"] == "add_admin":
            clean = val.lstrip("@")
            if clean.lstrip("-").isdigit():
                new_id = int(clean)
                _bot_admins.add(new_id)
                save_data()
                await update.message.reply_text(
                    f"✅ تم إضافة المشرف `{new_id}` بنجاح.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("❗ أرسل رقم المعرّف (User ID) فقط، مثل: `123456789`", parse_mode="Markdown")
                _pending_settings_input[user_id] = state
            return

        elif state["type"] == "set_limit":
            cid = state.get("chat_id")
            if val.isdigit():
                limit = int(val)
                _ai_daily_limit[cid] = limit
                save_data()
                name = _known_chat_names.get(cid, str(cid))
                if limit == 0:
                    msg = f"✅ تم إلغاء الحد اليومي للذكاء في مجموعة *{name}*."
                else:
                    msg = f"✅ تم تعيين حد *{limit}* طلب/يوم لمجموعة *{name}*."
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text("❗ أرسل رقماً صحيحاً (0 = بلا حد).")
                _pending_settings_input[user_id] = state
            return

        elif state["type"] == "rl_custom_count":
            target_id = state["target_id"]
            chat_id_rl = state["chat_id"]
            if not val.isdigit() or not (1 <= int(val) <= 9999):
                await update.message.reply_text("❗ أدخل رقماً بين 1 و 9999.")
                _pending_settings_input[user_id] = state
                return
            count = int(val)
            try:
                member = await context.bot.get_chat_member(chat_id_rl, target_id)
                target_name = member.user.full_name
            except Exception:
                target_name = str(target_id)
            kb = _build_rl_time_keyboard(target_id, chat_id_rl, count)
            await update.message.reply_text(
                f"✅ الحد: {count} رسالة\n\n⏱ اختر المدة الزمنية:",
                reply_markup=kb,
            )
            return

        elif state["type"] == "rl_custom_time":
            target_id = state["target_id"]
            chat_id_rl = state["chat_id"]
            count = state["count"]
            if not val.isdigit() or not (1 <= int(val) <= 99999):
                await update.message.reply_text("❗ أدخل رقماً بالدقائق (مثال: 90 = ساعة ونصف).")
                _pending_settings_input[user_id] = state
                return
            window_secs = int(val) * 60
            try:
                member = await context.bot.get_chat_member(chat_id_rl, target_id)
                target_name = member.user.full_name
            except Exception:
                target_name = str(target_id)
            msg = _activate_rate_limit_data(chat_id_rl, target_id, target_name, count, window_secs)
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

    # ============================================================
    # معالجة إدخال مفاتيح API الخاصة بمجموعة محددة
    # ============================================================
    if user_id and user_id in _pending_group_api_key_input:
        target_cid = _pending_group_api_key_input.pop(user_id)
        new_keys = [line.strip() for line in text.splitlines() if line.strip()]
        if not new_keys:
            await update.message.reply_text("❗ لم أجد أي مفاتيح. أرسل المفاتيح مرة أخرى، مفتاح في كل سطر.")
            _pending_group_api_key_input[user_id] = target_cid
            return
        grp_list = _group_gemini_keys.setdefault(target_cid, [])
        _group_exhausted_keys.pop(target_cid, None)
        added = 0
        for key in new_keys:
            if key not in grp_list:
                grp_list.append(key)
                added += 1
        save_data()
        grp_name = _known_chat_names.get(target_cid, str(target_cid))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔑 إدارة المفاتيح", callback_data=f"settings_grp_ai:{target_cid}"),
        ]])
        await update.message.reply_text(
            f"✅ تم إضافة *{added}* مفتاح لـ *{grp_name}*\n"
            f"📊 إجمالي مفاتيحها الآن: *{len(grp_list)}*",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    # ============================================================
    # معالجة إدخال مفاتيح API الجديدة من المالك
    # ============================================================
    if user_id and user_id in _pending_api_key_input:
        _pending_api_key_input.discard(user_id)
        new_keys = [line.strip() for line in text.splitlines() if line.strip()]
        if not new_keys:
            await update.message.reply_text("❗ لم أجد أي مفاتيح. أرسل المفاتيح مرة أخرى، مفتاح في كل سطر.")
            _pending_api_key_input.add(user_id)
            return
        added = 0
        for key in new_keys:
            if key not in _gemini_api_keys:
                _gemini_api_keys.append(key)
                added += 1
        save_data()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔑 عرض المفاتيح", callback_data="settings_api_keys"),
        ]])
        await update.message.reply_text(
            f"✅ تم إضافة {added} مفتاح جديد.\n"
            f"📊 إجمالي المفاتيح الآن: {len(_gemini_api_keys)}",
            reply_markup=keyboard,
        )
        return


    # ============================================================
    # معالجة إدخال مدة السشن المخصص
    # ============================================================
    if user_id and user_id in _pending_session_config:
        state = _pending_session_config[user_id]
        val = text.strip()
        if not val.isdigit() or not (1 <= int(val) <= 300):
            await update.message.reply_text("❗ أدخل رقماً صحيحاً بين 1 و 300.")
            return
        val = int(val)
        if state["step"] == "study":
            _pending_session_config[user_id] = {
                "step": "break",
                "chat_id": state["chat_id"],
                "study": val,
            }
            await update.message.reply_text(
                f"✅ <b>الدراسة: {val} دقيقة</b>\n\n📝 الآن أرسل مدة الاستراحة بالدقائق",
                parse_mode="HTML",
            )
            return
        elif state["step"] == "break":
            study = state["study"]
            break_t = val
            chat_id_target = state["chat_id"]
            del _pending_session_config[user_id]
            if _user_has_active_session(chat_id_target, user_id):
                await update.message.reply_text(
                    "⚠️ لديك سشن نشط بالفعل في هذه المجموعة.\n"
                    "أنهِ سشنك الحالي أولاً قبل بدء سشن جديد."
                )
                return
            if len(_sessions.get(chat_id_target, {})) >= _max_sessions:
                await update.message.reply_text(
                    f"عذراً، وصلنا للحد الأقصى ({_max_sessions} سشن) في هذه المجموعة 🚫"
                )
                return
            sess_id = _create_session(
                chat_id_target, study, break_t,
                user_id,
                update.effective_user.first_name,
                update.effective_user.username or "",
            )
            sess_text, sess_keyboard = build_session_message(chat_id_target, sess_id)
            msg = await context.bot.send_message(
                chat_id_target, sess_text, reply_markup=sess_keyboard, parse_mode="HTML"
            )
            _sessions[chat_id_target][sess_id]["message_id"] = msg.message_id
            return

    # ============================================================
    # معالجة حالة انتظار إضافة رد تلقائي
    # ============================================================
    if user_id and user_id in _pending_auto_reply:
        state = _pending_auto_reply[user_id]
        if state["step"] == "keyword":
            keyword = text.strip()
            _pending_auto_reply[user_id] = {
                "step": "reply",
                "chat_id": state["chat_id"],
                "keyword": keyword,
            }
            await update.message.reply_text(f"✅ الكلمة: «{keyword}»\n\nاكتب الرد الآن:")
            return
        elif state["step"] == "reply":
            keyword = state["keyword"]
            chat_id_target = state["chat_id"]
            reply_val = text.strip()
            if chat_id_target not in _auto_replies:
                _auto_replies[chat_id_target] = {}
            _auto_replies[chat_id_target][keyword.lower()] = reply_val
            del _pending_auto_reply[user_id]
            save_data()
            await update.message.reply_text(
                f"✅ تم إضافة الرد التلقائي:\n\nالكلمة: «{keyword}»\nالرد: {reply_val}"
            )
            return

    # ============================================================
    # 📊 فحص حد الرسائل — يعدّ رسائل الأعضاء ويقيّد عند التجاوز
    # ============================================================
    if chat and chat.type in ("group", "supergroup") and user_id:
        rl_key = f"{chat.id}_{user_id}"
        if rl_key in _rate_limits:
            rl = _rate_limits[rl_key]
            now = datetime.now()
            if now < rl["reset_time"]:
                rl["count"] += 1
                if rl["count"] > rl["limit"] and not rl.get("restricted"):
                    uname = update.effective_user.full_name if update.effective_user else str(user_id)
                    asyncio.create_task(
                        _apply_rate_limit_restriction(context.bot, chat.id, user_id, uname, rl["window_seconds"])
                    )
                    try:
                        await update.message.delete()
                    except Exception:
                        pass
                    return
            else:
                # انتهت النافذة — أعد العداد بدلاً من حذف الحد
                rl["count"] = 0
                rl["restricted"] = False
                rl["reset_time"] = datetime.now() + timedelta(seconds=rl["window_seconds"])

    action = None
    for cmd, act in ARABIC_COMMANDS.items():
        if text == cmd or text.startswith(cmd + " ") or text.startswith(cmd + "\n"):
            action = act
            break

    if action:
        handler = COMMAND_HANDLERS.get(action)
        if handler:
            await handler(update, context)
        return

    if detect_session_request(text):
        await show_session_setup(update, context)
        return

    # ============================================================
    # 🚫 كشف طلب منع التسخيت — يفتح لوحة الإعداد
    # ============================================================
    text_lower_f = text.lower()
    if (
        chat and chat.type in ("group", "supergroup")
        and any(tr in text_lower_f for tr in FOCUS_TRIGGERS)
        and user_id
    ):
        _focus_pending[user_id] = {
            "chat_id": chat.id,
            "minutes": None,
            "mode": "warn",
            "name": update.effective_user.first_name if update.effective_user else "المستخدم",
        }
        setup_text, setup_kb = build_focus_setup(user_id, chat.id)
        await update.message.reply_text(setup_text, reply_markup=setup_kb, parse_mode="Markdown")
        return

    # ============================================================
    # 🚫 تطبيق وضع منع التسخيت إذا العضو في جلسة نشطة
    # ============================================================
    if chat and chat.type in ("group", "supergroup") and user_id:
        chat_id_f = chat.id
        focus_map = _focus_sessions.get(chat_id_f, {})
        if user_id in focus_map:
            sess = focus_map[user_id]
            if datetime.now() < sess["until"]:
                mode = sess["mode"]
                name = sess.get("name", update.effective_user.first_name if update.effective_user else "")
                if mode == "warn":
                    sess["warn_msg_count"] = sess.get("warn_msg_count", 0) + 1
                    if sess["warn_msg_count"] % 5 == 1:
                        warning = random.choice(FOCUS_WARNINGS)
                        await update.message.reply_text(f"{name}، {warning}")
                elif mode == "delete":
                    try:
                        await update.message.delete()
                    except Exception:
                        pass
                elif mode == "mute":
                    if not sess.get("muted"):
                        remaining = sess["until"] - datetime.now()
                        try:
                            await context.bot.restrict_chat_member(
                                chat_id_f,
                                user_id,
                                ChatPermissions(can_send_messages=False),
                                until_date=datetime.now() + remaining,
                            )
                            sess["muted"] = True
                            try:
                                await update.message.delete()
                            except Exception:
                                pass
                        except Exception:
                            pass
                return
            else:
                focus_map.pop(user_id, None)

    youtube_query = detect_youtube_request(text)
    if youtube_query:
        await bot_youtube_response(update, context, youtube_query)
        return

    if is_calling_bot(text):
        replied = update.message.reply_to_message
        if (
            replied
            and replied.photo
            and replied.from_user
            and replied.from_user.id != context.bot.id
        ):
            await bot_reply_to_photo_response(update, context)
            return
        await bot_call_response(update, context)
        return

    if (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    ):
        await bot_call_response(update, context)
        return

    # ============================================================
    # فحص الردود التلقائية
    # ============================================================
    if update.effective_chat:
        chat_id_ar = update.effective_chat.id
        replies = _auto_replies.get(chat_id_ar, {})
        if replies:
            text_lower = text.lower()
            for keyword, auto_reply in replies.items():
                if _is_whole_word(keyword, text_lower):
                    await update.message.reply_text(auto_reply)
                    return

    # ============================================================
    # الردود المحفوظة — تحيات وصباح ومساء والخ (تلقائي بدون استدعاء البوت)
    # ============================================================
    if update.effective_chat and update.effective_user:
        _chat_for_fb = update.effective_chat
        _cid_fb = _chat_for_fb.id
        _allowed_fb = True
        if _chat_for_fb.type != "private":
            ok_fb, _ = is_ai_allowed_for_chat(_cid_fb)
            if not ok_fb:
                _allowed_fb = False
        if _allowed_fb:
            _first_fb = update.effective_user.first_name or "أخي"
            _fb_reply = get_smart_fallback(_first_fb, text)
            if _fb_reply:
                await update.message.reply_text(_fb_reply)
                return

    # await profanity_filter(update, context)  # موقوف مؤقتاً


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"✅ Health check server running on port {port}")


# ============================================================
# 🚫 نظام منع التسخيت
# ============================================================

_FOCUS_DUR_LABELS = {15: "15 د", 30: "30 د", 60: "ساعة", 90: "90 د", 120: "ساعتين", 180: "3 ساعات"}
_FOCUS_MODE_LABELS = {"warn": "⚠️ تحذير", "delete": "🗑 حذف", "mute": "🔇 كتم"}


def build_focus_setup(uid: int, chat_id: int) -> tuple:
    cfg = _focus_pending.get(uid, {"chat_id": chat_id, "minutes": None, "mode": "warn"})
    minutes = cfg.get("minutes")
    mode = cfg.get("mode", "warn")

    def dlbl(m):
        lbl = _FOCUS_DUR_LABELS.get(m, f"{m} د")
        return f"✅ {lbl}" if minutes == m else lbl

    def mlbl(m):
        lbl = _FOCUS_MODE_LABELS[m]
        return f"✅ {lbl}" if mode == m else lbl

    mins_ar = _FOCUS_DUR_LABELS.get(minutes, f"{minutes} دقيقة") if minutes else "_لم تحدد بعد_"
    mode_ar = _FOCUS_MODE_LABELS.get(mode, "⚠️ تحذير")

    mode_desc = {
        "warn": "البوت يرد بتحذير مضحك على كل رسالة",
        "delete": "البوت يحذف رسائلك فوراً",
        "mute": "يُكتم عند أول رسالة ويُفك الكتم بعد انتهاء المدة",
    }.get(mode, "")

    text = (
        "🚫 *إعداد منع التسخيت*\n\n"
        f"⏳ المدة: {mins_ar}\n"
        f"⚙️ الوضع: {mode_ar}\n"
        f"_{mode_desc}_\n\n"
        "اختر المدة والوضع ثم اضغط ابدأ:"
    )

    dur_row1 = [
        InlineKeyboardButton(dlbl(15), callback_data=f"focus_d:{uid}:15"),
        InlineKeyboardButton(dlbl(30), callback_data=f"focus_d:{uid}:30"),
        InlineKeyboardButton(dlbl(60), callback_data=f"focus_d:{uid}:60"),
    ]
    dur_row2 = [
        InlineKeyboardButton(dlbl(90), callback_data=f"focus_d:{uid}:90"),
        InlineKeyboardButton(dlbl(120), callback_data=f"focus_d:{uid}:120"),
        InlineKeyboardButton(dlbl(180), callback_data=f"focus_d:{uid}:180"),
    ]
    mode_row = [
        InlineKeyboardButton(mlbl("warn"), callback_data=f"focus_m:{uid}:warn"),
        InlineKeyboardButton(mlbl("delete"), callback_data=f"focus_m:{uid}:delete"),
        InlineKeyboardButton(mlbl("mute"), callback_data=f"focus_m:{uid}:mute"),
    ]
    action_row = []
    if minutes:
        action_row.append(InlineKeyboardButton("🚫 ابدأ منع التسخيت", callback_data=f"focus_start:{uid}"))
    action_row.append(InlineKeyboardButton("❌ إلغاء", callback_data=f"focus_cancel:{uid}"))

    return text, InlineKeyboardMarkup([dur_row1, dur_row2, mode_row, action_row])


async def run_focus_timer(chat_id: int, user_id: int, minutes: int, name: str, bot):
    """مؤقت جلسة منع التسخيت — يعمل في الخلفية."""
    await asyncio.sleep(minutes * 60)

    session = _focus_sessions.get(chat_id, {}).pop(user_id, None)
    if not _focus_sessions.get(chat_id):
        _focus_sessions.pop(chat_id, None)
    if not session:
        return

    if session.get("muted"):
        try:
            await bot.restrict_chat_member(
                chat_id,
                user_id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_send_polls=True,
                    can_invite_users=True,
                ),
            )
        except Exception:
            pass

    try:
        await bot.send_message(
            chat_id,
            f"🎉 {name}، انتهت جلسة منع التسخيت!\n"
            f"يمكنك الكلام الآن — أتمنى تكون استفدت!",
        )
    except Exception:
        pass


async def handle_focus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج أزرار إعداد وبدء جلسة منع التسخيت."""
    query = update.callback_query
    data = query.data
    parts = data.split(":")
    action = parts[0]
    uid = int(parts[1])

    if query.from_user.id != uid:
        await query.answer("❌ هذا الزر مو لك!", show_alert=True)
        return

    await query.answer()
    chat_id = query.message.chat_id
    user = query.from_user

    if uid not in _focus_pending:
        _focus_pending[uid] = {
            "chat_id": chat_id,
            "minutes": None,
            "mode": "warn",
            "name": user.first_name or "المستخدم",
        }

    if action == "focus_d":
        _focus_pending[uid]["minutes"] = int(parts[2])

    elif action == "focus_m":
        _focus_pending[uid]["mode"] = parts[2]

    elif action == "focus_cancel":
        _focus_pending.pop(uid, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif action == "focus_start":
        cfg = _focus_pending.pop(uid, None)
        if not cfg or not cfg.get("minutes"):
            await query.answer("⚠️ اختر المدة أولاً!", show_alert=True)
            _focus_pending[uid] = cfg or {"chat_id": chat_id, "minutes": None, "mode": "warn"}
            return

        minutes = cfg["minutes"]
        mode = cfg.get("mode", "warn")
        name = cfg.get("name", user.first_name or "المستخدم")
        until = datetime.now() + timedelta(minutes=minutes)

        # إلغاء أي جلسة سابقة للمستخدم
        old = _focus_sessions.get(chat_id, {}).pop(uid, None)
        if old and old.get("task"):
            old["task"].cancel()

        task = asyncio.create_task(run_focus_timer(chat_id, uid, minutes, name, context.bot))
        if chat_id not in _focus_sessions:
            _focus_sessions[chat_id] = {}
        _focus_sessions[chat_id][uid] = {
            "until": until,
            "mode": mode,
            "task": task,
            "muted": False,
            "name": name,
        }

        dur_ar = _FOCUS_DUR_LABELS.get(minutes, f"{minutes} دقيقة")
        mode_ar = _FOCUS_MODE_LABELS.get(mode, mode)
        mode_note = {
            "warn": "📢 سأرد على كل رسالة ترسلها بتحذير",
            "delete": "🗑 سأحذف أي رسالة ترسلها فوراً",
            "mute": "🔇 سأكتمك عند أول رسالة ترسلها حتى انتهاء المدة",
        }.get(mode, "")

        try:
            await query.message.edit_text(
                f"🚫 *منع التسخيت بدأ!*\n\n"
                f"👤 {name}\n"
                f"⏳ المدة: {dur_ar}\n"
                f"⚙️ الوضع: {mode_ar}\n\n"
                f"_{mode_note}_\n\n"
                f"حظ موفق في الدراسة! 📚",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # تحديث رسالة الإعداد بعد كل تغيير
    text, keyboard = build_focus_setup(uid, chat_id)
    try:
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        pass


async def handle_key_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج الضغط على أزرار حالة المفاتيح — للعرض فقط."""
    query = update.callback_query
    if query.from_user.id != OWNER_CHAT_ID:
        await query.answer()
        return
    await query.answer()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if "Conflict" in str(error) or "409" in str(error):
        logger.warning("⚠️ تعارض نسختين من البوت — سيتم الانتظار والمحاولة مجدداً...")
        return
    logger.error(f"خطأ غير متوقع: {error}")


def main():
    global gemini_client
    load_data()

    # إعادة تهيئة gemini_client بعد تحميل المفاتيح من قاعدة البيانات
    if _gemini_api_keys and gemini_client is None:
        gemini_client = _make_gemini_client(_gemini_api_keys[0])

    if not BOT_TOKEN:
        logger.error("❌ متغير البيئة مفقود: TELEGRAM_BOT_TOKEN")
        logger.error("⛔ البوت لن يعمل. أضف TELEGRAM_BOT_TOKEN وأعد التشغيل.")
        return

    if not _gemini_api_keys:
        logger.warning("⚠️ لم يتم العثور على أي مفتاح Gemini — ميزة الذكاء الاصطناعي معطّلة.")

    async def _periodic_save():
        """يحفظ البيانات تلقائياً كل 5 دقائق."""
        while True:
            await asyncio.sleep(300)
            save_data()

    async def _post_init(application):
        global _bot_app, _bot_loop
        _bot_app = application
        _bot_loop = asyncio.get_event_loop()
        asyncio.create_task(_periodic_save())
        await _restore_sessions_from_db(application.bot)
        # أرسل/حدّث رسالة الحالة عند كل تشغيل لتعكس المفاتيح الحالية
        _schedule_update_status_message()

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_yt_pick, pattern=r"^yt_pick_"))
    app.add_handler(CallbackQueryHandler(handle_yt_format, pattern=r"^yt_fmt_"))
    app.add_handler(CallbackQueryHandler(handle_key_status_callback, pattern=r"^key_status_"))
    app.add_handler(CallbackQueryHandler(handle_session_callback, pattern=r"^sess_"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^settings_"))
    app.add_handler(CallbackQueryHandler(handle_focus_callback, pattern=r"^focus_"))
    app.add_handler(CallbackQueryHandler(handle_rate_limit_callback, pattern=r"^rl_"))
    app.add_handler(CallbackQueryHandler(handle_stats_callback, pattern=r"^(statsperiod|statsuser):"))
    app.add_handler(CallbackQueryHandler(handle_help_callback, pattern=r"^help_"))
    app.add_handler(ChatMemberHandler(handle_owner_protection, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.VIDEO, media_handler_video))
    app.add_handler(MessageHandler(filters.Document.ALL, media_handler_document))
    app.add_handler(MessageHandler(filters.Sticker.ALL, media_handler_sticker))
    app.add_handler(MessageHandler(filters.ANIMATION, media_handler_animation))
    app.add_handler(MessageHandler(filters.VOICE, media_handler_voice))
    app.add_handler(MessageHandler(filters.AUDIO, media_handler_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    start_health_server()
    logger.info("✅ البوت يعمل الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
