"""Campaign hierarchy, and root_campaign added NULLABLE.

Deliberately split across three migrations -- 0010 schema, 0011 backfill, 0012
tighten -- rather than the single file `makemigrations` produces. Adding a NOT
NULL column with a unique constraint in one step cannot work against a table
that already has rows, and keeping the backfill in its own file gives a safe
point to cut a deploy at.

NOTHING HERE CREATES A PARENT LINK, and that is what makes 0012 provably safe.
See the note in that file before writing a migration that reparents anything.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crm", "0009_gmailcredential")]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="parent",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sub_campaigns", to="crm.campaign",
                help_text="The root campaign this personalises. Blank for a root campaign.",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="owner",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sub_campaigns", to="crm.teammember",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="footer",
            field=models.TextField(
                blank=True,
                help_text="Your sign-off. Appended to the campaign body in your mail only.",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="footer_is_html",
            field=models.BooleanField(
                default=False, help_text="Write the footer as raw HTML. Leads only."
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.UniqueConstraint(
                condition=models.Q(("parent__isnull", False)),
                fields=("parent", "owner"),
                name="uniq_parent_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("owner__isnull", True), ("parent__isnull", True))
                    | models.Q(("owner__isnull", False), ("parent__isnull", False))
                ),
                name="campaign_parent_and_owner_agree",
            ),
        ),
        # Nullable for now. 0011 fills it, 0012 forbids the null.
        migrations.AddField(
            model_name="campaignmailing",
            name="root_campaign",
            field=models.ForeignKey(
                null=True, db_index=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="root_mailings", to="crm.campaign",
            ),
        ),
    ]
