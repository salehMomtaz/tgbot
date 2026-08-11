"""
modules.admin package — public API re-exports from sub-modules.

This package replaces the original modules/admin.py (1833 lines).
All original import paths continue to work unchanged.
"""

from .keyboards import (
    build_console_keyboard,
    get_premium_menu_keyboard,
    get_cookie_action_keyboard,
    get_pot_menu_keyboard,
    get_direct_menu_keyboard,
    _gen_abort_markup,
    _gen_dial_pad_markup,
)

from .state import (
    USER_STATES,
    ACTIVE_PROMPTS,
    PREMIUM_GEN,
    _PREMIUM_GEN_TTL,
)

from .premium_gen import (
    sweep_stale_generations,
    _premium_gen_cleanup,
    _premium_gen_pad_text,
    _handle_premium_gen_input,
    _finish_premium_gen,
    _time_monotonic,
    discard_client_quiet,
    _purge_active_prompt,
)

from .cookies import (
    COOKIE_MAP,
    _has_real_cookie_line,
    _write_cookie_jar,
)

from .cookie_test import (
    _run_cookie_test_sync,
    _test_cookie_jar,
)

from .pot_menu import (
    _pot_running,
    _render_pot_menu,
    _handle_pot_action,
)

from .direct_menu import (
    _render_direct_menu,
)

from .callback_dispatch import (
    _admin_callback_dispatch,
    back_markup,
)

from .register import (
    register_admin_handlers,
)

__all__ = [
    # keyboards
    "build_console_keyboard",
    "get_premium_menu_keyboard",
    "get_cookie_action_keyboard",
    "get_pot_menu_keyboard",
    "get_direct_menu_keyboard",
    "_gen_abort_markup",
    "_gen_dial_pad_markup",
    # state
    "USER_STATES",
    "ACTIVE_PROMPTS",
    "PREMIUM_GEN",
    "_PREMIUM_GEN_TTL",
    # premium_gen
    "sweep_stale_generations",
    "_premium_gen_cleanup",
    "_premium_gen_pad_text",
    "_handle_premium_gen_input",
    "_finish_premium_gen",
    "_time_monotonic",
    "discard_client_quiet",
    "_purge_active_prompt",
    # cookies
    "COOKIE_MAP",
    "_has_real_cookie_line",
    "_write_cookie_jar",
    # cookie_test
    "_run_cookie_test_sync",
    "_test_cookie_jar",
    # pot_menu
    "_pot_running",
    "_render_pot_menu",
    "_handle_pot_action",
    # direct_menu
    "_render_direct_menu",
    # callback_dispatch
    "_admin_callback_dispatch",
    "back_markup",
    # register
    "register_admin_handlers",
]