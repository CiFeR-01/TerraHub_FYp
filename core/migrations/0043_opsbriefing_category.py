# Generated for TerraHub Phase 4 - see ANALYTICS_CHANGELOG.md.
#
# Adds OpsBriefing.category (materials/products/sales/purchase/logistics/
# warehouse/my_checklist), splitting the single Phase 3 briefing into the
# category-scoped briefings + the personal checklist. Existing rows (all from
# Phase 3 testing, not real operational history) get the one-off 'legacy'
# value and become invisible to every new category/checklist view -
# preserve_default=False means models.py itself carries no default, so every
# future .create() call must pass category explicitly.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0042_seed_ops_briefing_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='opsbriefing',
            name='category',
            field=models.CharField(
                choices=[
                    ('legacy', 'Legacy (pre-category)'),
                    ('materials', 'Materials'),
                    ('products', 'Products'),
                    ('sales', 'Sales'),
                    ('purchase', 'Purchase'),
                    ('logistics', 'Logistics'),
                    ('warehouse', 'Warehouse'),
                    ('my_checklist', 'My Checklist'),
                ],
                db_index=True,
                default='legacy',
                max_length=20,
            ),
            preserve_default=False,
        ),
        # help_text-only change (rewritten alongside the category split); no
        # column/type change.
        migrations.AlterField(
            model_name='opsbriefing',
            name='generated_by',
            field=models.ForeignKey(
                blank=True,
                help_text="Who triggered a manual run (always set for 'my_checklist' - "
                          "it is who the checklist is for); null for a scheduled category run.",
                null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name='opsbriefing',
            index=models.Index(fields=['category', '-generated_at'], name='core_opsbri_categor_1755f2_idx'),
        ),
        migrations.AddIndex(
            model_name='opsbriefing',
            index=models.Index(fields=['category', 'generated_by', '-generated_at'], name='core_opsbri_categor_8064b0_idx'),
        ),
    ]
