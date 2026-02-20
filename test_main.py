"""
Тести для рефакторингу monitor bot.
Запуск: python -m pytest tests.py -v
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch


# ── Імпортуємо лише чисті функції (без Telethon/asyncio startup) ──
import importlib, sys, types

# Мінімальний stub щоб обійти запуск asyncio.run(main()) при імпорті
telethon_stub = types.ModuleType("telethon")
telethon_stub.TelegramClient = MagicMock()
telethon_stub.events = MagicMock()
telethon_events = types.ModuleType("telethon.events")
telethon_events.NewMessage = MagicMock(return_value=lambda f: f)
telethon_stub.events = telethon_events
telethon_tl = types.ModuleType("telethon.tl")
telethon_tl_funcs = types.ModuleType("telethon.tl.functions")
telethon_tl_channels = types.ModuleType("telethon.tl.functions.channels")
telethon_tl_channels.JoinChannelRequest  = MagicMock()
telethon_tl_channels.LeaveChannelRequest = MagicMock()
telethon_errors = types.ModuleType("telethon.errors")
telethon_errors.FloodWaitError = Exception

sys.modules.setdefault("telethon",                          telethon_stub)
sys.modules.setdefault("telethon.events",                   telethon_events)
sys.modules.setdefault("telethon.tl",                       telethon_tl)
sys.modules.setdefault("telethon.tl.functions",             telethon_tl_funcs)
sys.modules.setdefault("telethon.tl.functions.channels",    telethon_tl_channels)
sys.modules.setdefault("telethon.errors",                   telethon_errors)

# Задаємо env щоб не впав при перевірці
import os
os.environ.setdefault("TG_API_ID",   "12345678")
os.environ.setdefault("TG_API_HASH", "deadbeef")
os.environ.setdefault("TG_PHONE",    "+34600000000")

# Патчимо asyncio.run щоб main() не запустилась при імпорті
with patch("asyncio.run"):
    # Патчимо LOCK_FILE щоб не створювати файл
    with patch("pathlib.Path.exists", return_value=False), \
         patch("pathlib.Path.write_text"):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "main_module",
            pathlib.Path(__file__).parent / "main.py"
        )
        main_module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(main_module)
        except SystemExit:
            pass

clean_minus_words = main_module.clean_minus_words
has_minus_word    = main_module.has_minus_word
find_keyword      = main_module.find_keyword
format_sender     = main_module.format_sender
format_chat       = main_module.format_chat
is_admin          = main_module.is_admin


# ════════════════════════════════════════════════════════════════
# clean_minus_words
# ════════════════════════════════════════════════════════════════
class TestCleanMinusWords:
    def test_removes_skip_words_from_minus(self):
        minus  = ["buy usdt now"]
        skip   = ["buy", "now"]
        result = clean_minus_words(minus, skip, [])
        assert result == ["usdt"]

    def test_removes_keyword_from_minus(self):
        minus    = ["cita garantizado"]
        keywords = ["cita"]
        result   = clean_minus_words(minus, [], keywords)
        assert result == ["garantizado"]

    def test_deduplication(self):
        minus  = ["spam", "SPAM", "Spam"]
        result = clean_minus_words(minus, [], [])
        assert len(result) == 1

    def test_removes_empty_after_cleaning(self):
        minus  = ["the"]
        skip   = ["the"]
        result = clean_minus_words(minus, skip, [])
        assert result == []

    def test_preserves_untouched_entries(self):
        minus  = ["garantizado", "скидка"]
        result = clean_minus_words(minus, ["the"], [])
        assert result == ["garantizado", "скидка"]

    def test_empty_lists(self):
        assert clean_minus_words([], [], []) == []

    def test_multiword_minus_partial_removal(self):
        minus  = ["buy usdt p2p"]
        skip   = ["buy", "p2p"]
        result = clean_minus_words(minus, skip, [])
        assert result == ["usdt"]


# ════════════════════════════════════════════════════════════════
# has_minus_word
# ════════════════════════════════════════════════════════════════
class TestHasMinusWord:
    def test_detects_phrase(self):
        assert has_minus_word("Помогите получить сиру garantizado!", ["garantizado"])

    def test_detects_multi_word_phrase(self):
        assert has_minus_word("comprar usdt p2p deal", ["usdt p2p"])

    def test_no_match(self):
        assert not has_minus_word("Помогите с ситой пожалуйста", ["garantizado", "bitcoin"])

    def test_case_insensitive(self):
        assert has_minus_word("GARANTIZADO resultado", ["garantizado"])

    def test_empty_minus(self):
        assert not has_minus_word("будь-яке повідомлення", [])

    def test_empty_text(self):
        assert not has_minus_word("", ["spam"])


# ════════════════════════════════════════════════════════════════
# find_keyword
# ════════════════════════════════════════════════════════════════
class TestFindKeyword:
    KEYWORDS = ["cita previa", "tarjeta", "renovar", "тома де уельяс"]

    def test_finds_exact_keyword(self):
        assert find_keyword("Necesito una cita previa urgente", self.KEYWORDS) == "cita previa"

    def test_finds_single_word_keyword(self):
        assert find_keyword("Mi tarjeta expiró", self.KEYWORDS) == "tarjeta"

    def test_returns_none_when_not_found(self):
        assert find_keyword("Hola, ¿cómo estás?", self.KEYWORDS) is None

    def test_case_insensitive_match(self):
        assert find_keyword("RENOVAR mi NIE", self.KEYWORDS) == "renovar"

    def test_cyrillic_keyword(self):
        assert find_keyword("помогите тома де уельяс пожалуйста", self.KEYWORDS) == "тома де уельяс"

    def test_partial_word_no_match(self):
        # "tarjetita" — не повне слово "tarjeta" за word-boundary... але "tarjeta" є в "tarjetita"
        # Перевіряємо поведінку з \b
        result = find_keyword("tengo tarjetita", ["tarjeta"])
        # \b не спрацює між "tarjeta" і "ita", тому None
        assert result is None

    def test_returns_first_match(self):
        kw  = ["renovar", "renovar nie"]
        res = find_keyword("quiero renovar nie", kw)
        assert res in kw   # будь-яке зі збігів

    def test_empty_text(self):
        assert find_keyword("", self.KEYWORDS) is None

    def test_empty_keywords(self):
        assert find_keyword("cita previa", []) is None


# ════════════════════════════════════════════════════════════════
# format_sender
# ════════════════════════════════════════════════════════════════
class TestFormatSender:
    def _make_sender(self, first="", last="", username=None, uid=None):
        s = MagicMock()
        s.first_name = first
        s.last_name  = last
        s.username   = username
        s.id         = uid
        return s

    def test_full_name_with_username(self):
        s = self._make_sender("Іван", "Петров", username="ivanp")
        assert "Іван Петров" in format_sender(s)
        assert "@ivanp" in format_sender(s)

    def test_only_first_name_no_username(self):
        s = self._make_sender("Марія", uid=123456)
        result = format_sender(s)
        assert "Марія" in result
        assert "123456" in result

    def test_no_name_no_username_has_id(self):
        s = self._make_sender(uid=9999)
        assert "9999" in format_sender(s)

    def test_all_empty(self):
        s = self._make_sender()
        result = format_sender(s)
        assert result == ""  # нічого не заповнено


# ════════════════════════════════════════════════════════════════
# format_chat
# ════════════════════════════════════════════════════════════════
class TestFormatChat:
    def _make_chat(self, title="", username=None):
        c = MagicMock()
        c.title    = title
        c.username = username
        return c

    def test_title_with_username(self):
        c = self._make_chat("Міграція Іспанія", "migracia_es")
        result = format_chat(c)
        assert "Міграція Іспанія" in result
        assert "@migracia_es" in result

    def test_title_without_username(self):
        c = self._make_chat("Закрита Група")
        result = format_chat(c)
        assert "Закрита Група" in result
        assert "@" not in result


# ════════════════════════════════════════════════════════════════
# is_admin
# ════════════════════════════════════════════════════════════════
class TestIsAdmin:
    ADMINS = ["@Bogdan_Bubra", "@katrazhenko"]

    def test_valid_admin(self):
        assert is_admin("bogdan_bubra", self.ADMINS)

    def test_case_insensitive(self):
        assert is_admin("KATRAZHENKO", self.ADMINS)

    def test_not_admin(self):
        assert not is_admin("randomuser", self.ADMINS)

    def test_empty_admins(self):
        assert not is_admin("bogdan_bubra", [])


# ════════════════════════════════════════════════════════════════
# Інтеграційні: clean_minus + has_minus
# ════════════════════════════════════════════════════════════════
class TestIntegration:
    def test_after_cleaning_minus_no_false_positive(self):
        """Після очищення 'cita garantizado' -> 'garantizado',
           повідомлення з 'cita' не блокується мінус-словом."""
        raw_minus = ["cita garantizado"]
        keywords  = ["cita"]
        cleaned   = clean_minus_words(raw_minus, [], keywords)
        # cleaned == ["garantizado"]
        msg = "Necesito una cita previa"
        assert not has_minus_word(msg, cleaned), (
            "Після очищення повідомлення з keyword не має блокуватись"
        )

    def test_spam_still_blocked_after_cleaning(self):
        raw_minus = ["garantizado dinero rápido"]
        skip      = ["dinero"]
        cleaned   = clean_minus_words(raw_minus, skip, [])
        # cleaned == ["garantizado rápido"]
        msg = "Asilo garantizado rápido sin papeles"
        assert has_minus_word(msg, cleaned)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
