"""Business logic for user-service (re-exported for the routers)."""

from app.services.users import (
    DEFAULT_DISPLAY_NAME,
    display_name_from_claims,
    get_or_create_user,
    get_user_by_id,
    get_user_by_sub,
    get_user_or_404,
    resolve_users,
    search_users,
    set_avatar,
    update_profile,
)
from app.services.voice import (
    delete_profile,
    enroll,
    get_profile,
    list_profiles,
    list_profiles_for_campaign,
    split_uri,
)

__all__ = [
    "DEFAULT_DISPLAY_NAME",
    "delete_profile",
    "display_name_from_claims",
    "enroll",
    "get_or_create_user",
    "get_profile",
    "get_user_by_id",
    "get_user_by_sub",
    "get_user_or_404",
    "list_profiles",
    "list_profiles_for_campaign",
    "resolve_users",
    "search_users",
    "set_avatar",
    "split_uri",
    "update_profile",
]
