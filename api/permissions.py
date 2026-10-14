from rest_framework_api_key.models import APIKey
from rest_framework_api_key.permissions import HasAPIKey, KeyParser


class HasValidAPIKey(HasAPIKey):
    """The library's check, with a message that says what to send."""

    message = 'Missing or invalid API key. Send the header: Authorization: Api-Key <key>'


def api_key_of(request):
    """The APIKey a request was made with, or None."""
    key = KeyParser().get(request)
    if not key:
        return None
    try:
        return APIKey.objects.get_from_key(key)
    except APIKey.DoesNotExist:
        return None
