"""Point every existing mailing at its own campaign as its root.

Every campaign that exists before 0010 is a root -- 0010 creates no parent
links -- so `root_campaign_id := campaign_id` is the identity map. That is why
the unique constraint in 0012 is provably safe to add: it is a unique index over
the image of an injective map on a set `uniq_campaign_contact` already
guarantees is unique. It cannot collide.

Done in SQL rather than by iterating the queryset: this is one UPDATE over a
table that could hold tens of thousands of rows, and pulling them through Python
over the Supabase pooler to write back a value we already have would be slow for
no reason.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    schema_editor.execute(
        "UPDATE campaign_mailings SET root_campaign_id = campaign_id "
        "WHERE root_campaign_id IS NULL"
    )


def unfill(apps, schema_editor):
    # Reversible so 0012 can be rolled back through. Clearing the column loses
    # nothing: 0010's forward step is what re-derives it.
    schema_editor.execute("UPDATE campaign_mailings SET root_campaign_id = NULL")


class Migration(migrations.Migration):

    dependencies = [("crm", "0010_campaign_hierarchy")]

    operations = [migrations.RunPython(backfill, unfill)]
