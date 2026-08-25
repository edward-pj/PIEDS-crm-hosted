"""Revoke every live API token; the endpoints they authenticated are gone.

The routes were deleted in the same change, so a token already reaches nothing.
This is belt and braces, and it is worth having: a token sitting in a `.env` on
somebody's laptop -- or in a screenshot, or a chat log -- must not become live
again if a route is ever reintroduced by accident.

The TABLE is deliberately kept. Dropping it is the one migration with no cheap
rollback, and `ApiToken` rows are a record of who was issued what and when,
which is worth keeping until the cutover is proven. Drop it in a later cleanup.
"""

from django.db import migrations
from django.utils import timezone


def revoke_all(apps, schema_editor):
    ApiToken = apps.get_model("crm", "ApiToken")
    ApiToken.objects.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


def unrevoke(apps, schema_editor):
    """Deliberately a no-op.

    Un-revoking would hand working credentials back to laptops that should no
    longer have them, and a migration that rolls back INTO a weaker security
    posture is a trap. Rolling back past this leaves the tokens revoked, which
    is the safe direction to fail in.
    """


class Migration(migrations.Migration):

    dependencies = [("crm", "0014_seed_default_team")]

    operations = [migrations.RunPython(revoke_all, unrevoke)]
