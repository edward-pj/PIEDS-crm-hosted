"""Forbid a null root, then enforce one mail per contact per root.

**Why NOT NULL is load-bearing rather than tidiness.** Postgres unique indexes
treat NULLs as distinct, so with a nullable column two rows with a null root and
the same contact would both insert cleanly. A nullable root is not a weaker
guarantee than a NOT NULL one -- it is no guarantee at all. `check_db` asserts
this against information_schema for the same reason.

**The safety proof, and the corollary that outlives it.** 0011's backfill is the
identity map on a pair `uniq_campaign_contact` already guarantees unique, so
this AddConstraint cannot fail on existing data. That holds ONLY because no
migration creates a parent link. The moment two existing campaigns are reparented
under one root, every contact both of them mailed collapses to a single
(root, contact) pair and the insert fails.

So: migrations must never create hierarchy -- it is authored through the app --
and `campaigns.set_parent()` runs a collision query and refuses first. There is
no database-level way to catch it at reparent time; the constraint fires on the
NEXT insert, long after the damage.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crm", "0011_backfill_root_campaign")]

    operations = [
        migrations.AlterField(
            model_name="campaignmailing",
            name="root_campaign",
            field=models.ForeignKey(
                db_index=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="root_mailings", to="crm.campaign",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaignmailing",
            constraint=models.UniqueConstraint(
                fields=("root_campaign", "contact"),
                name="uniq_root_campaign_contact",
            ),
        ),
    ]
