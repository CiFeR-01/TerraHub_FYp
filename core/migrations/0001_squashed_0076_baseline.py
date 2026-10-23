"""
Baseline: the whole schema as of 0076, replacing migrations 0001-0076.

Fresh databases (new installs, the test DB) run only this file. Databases that already
applied 0001-0076 (Railway, older local DBs) treat it as applied and skip it.
Keep the old 0001-0076 files until every database has reached 0076; then they can be deleted.

Besides the schema it seeds what the old data migrations left on a fresh database:
- SystemSetting rows from core.settings_store.REGISTRY (was 0038/0039/0042/0044/0055/0062/0064)
- the role Groups and their permissions as they stood after 0060/0069/0070/0076
  (Purchaser already folded into Purchasing).
The old one-off backfills (0015, 0027, 0032, 0036, 0046, 0047, 0049, 0050, 0052, 0058) only
fixed existing rows, so they have nothing to do on a fresh database and are left out.
"""

import core.validators
import django.contrib.auth.models
import django.contrib.auth.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.management import create_contenttypes
from django.db import migrations, models

from core.settings_store import REGISTRY


# Codenames (app core) per role, as the old migrations left them.
GROUP_PERMS = {
    'Admin': [
        'add_batch',
        'add_client',
        'add_customuser',
        'add_material',
        'add_notification',
        'add_opsbriefing',
        'add_ordertimeline',
        'add_product',
        'add_productionconsumption',
        'add_productionrun',
        'add_productionrunyieldlog',
        'add_productrecipe',
        'add_purchaseorder',
        'add_purchaseorderdetail',
        'add_registrylog',
        'add_role',
        'add_runmaterialusage',
        'add_salesorder',
        'add_salesorderdetail',
        'add_shipment',
        'add_shipmentitem',
        'add_shipmentitemreceipt',
        'add_stockallocation',
        'add_stockaudit',
        'add_supplier',
        'add_suppliermaterial',
        'add_systemsetting',
        'add_warehouse',
        'add_warehouselocation',
        'add_warehouseutilizationsnapshot',
        'adjust_physical_stock',
        'allocate_order_stock',
        'allocate_run_materials',
        'approve_production_runs',
        'approve_purchase_orders',
        'approve_sales_orders',
        'approve_shipments',
        'cancel_orders',
        'change_batch',
        'change_client',
        'change_customuser',
        'change_material',
        'change_notification',
        'change_opsbriefing',
        'change_ordertimeline',
        'change_product',
        'change_productionconsumption',
        'change_productionrun',
        'change_productionrunyieldlog',
        'change_productrecipe',
        'change_purchaseorder',
        'change_purchaseorderdetail',
        'change_registrylog',
        'change_role',
        'change_runmaterialusage',
        'change_salesorder',
        'change_salesorderdetail',
        'change_shipment',
        'change_shipmentitem',
        'change_shipmentitemreceipt',
        'change_stockallocation',
        'change_stockaudit',
        'change_supplier',
        'change_suppliermaterial',
        'change_systemsetting',
        'change_warehouse',
        'change_warehouselocation',
        'change_warehouseutilizationsnapshot',
        'delete_batch',
        'delete_client',
        'delete_customuser',
        'delete_material',
        'delete_notification',
        'delete_opsbriefing',
        'delete_ordertimeline',
        'delete_product',
        'delete_productionconsumption',
        'delete_productionrun',
        'delete_productionrunyieldlog',
        'delete_productrecipe',
        'delete_purchaseorder',
        'delete_purchaseorderdetail',
        'delete_registrylog',
        'delete_role',
        'delete_runmaterialusage',
        'delete_salesorder',
        'delete_salesorderdetail',
        'delete_shipment',
        'delete_shipmentitem',
        'delete_shipmentitemreceipt',
        'delete_stockallocation',
        'delete_stockaudit',
        'delete_supplier',
        'delete_suppliermaterial',
        'delete_systemsetting',
        'delete_warehouse',
        'delete_warehouselocation',
        'delete_warehouseutilizationsnapshot',
        'manage_users',
        'override_approvals',
        'set_order_status',
        'view_batch',
        'view_client',
        'view_customuser',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_role',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_systemsetting',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Logistics': [
        'add_ordertimeline',
        'add_shipment',
        'add_shipmentitem',
        'add_shipmentitemreceipt',
        'change_ordertimeline',
        'change_shipment',
        'change_shipmentitem',
        'change_shipmentitemreceipt',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Manager': [
        'add_batch',
        'add_client',
        'add_material',
        'add_notification',
        'add_opsbriefing',
        'add_ordertimeline',
        'add_product',
        'add_productionconsumption',
        'add_productionrun',
        'add_productionrunyieldlog',
        'add_productrecipe',
        'add_purchaseorder',
        'add_purchaseorderdetail',
        'add_registrylog',
        'add_runmaterialusage',
        'add_salesorder',
        'add_salesorderdetail',
        'add_shipment',
        'add_shipmentitem',
        'add_shipmentitemreceipt',
        'add_stockallocation',
        'add_stockaudit',
        'add_supplier',
        'add_suppliermaterial',
        'add_warehouse',
        'add_warehouselocation',
        'add_warehouseutilizationsnapshot',
        'allocate_order_stock',
        'allocate_run_materials',
        'approve_production_runs',
        'approve_purchase_orders',
        'approve_sales_orders',
        'approve_shipments',
        'cancel_orders',
        'change_batch',
        'change_client',
        'change_material',
        'change_notification',
        'change_opsbriefing',
        'change_ordertimeline',
        'change_product',
        'change_productionconsumption',
        'change_productionrun',
        'change_productionrunyieldlog',
        'change_productrecipe',
        'change_purchaseorder',
        'change_purchaseorderdetail',
        'change_registrylog',
        'change_runmaterialusage',
        'change_salesorder',
        'change_salesorderdetail',
        'change_shipment',
        'change_shipmentitem',
        'change_shipmentitemreceipt',
        'change_stockallocation',
        'change_stockaudit',
        'change_supplier',
        'change_suppliermaterial',
        'change_warehouse',
        'change_warehouselocation',
        'change_warehouseutilizationsnapshot',
        'delete_stockallocation',
        'manage_users',
        'set_order_status',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Manufacturing': [
        'add_batch',
        'add_ordertimeline',
        'add_productionconsumption',
        'add_productionrun',
        'add_productionrunyieldlog',
        'add_productrecipe',
        'add_runmaterialusage',
        'add_stockallocation',
        'allocate_run_materials',
        'change_batch',
        'change_ordertimeline',
        'change_productionconsumption',
        'change_productionrun',
        'change_productionrunyieldlog',
        'change_productrecipe',
        'change_runmaterialusage',
        'change_stockallocation',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Purchasing': [
        'add_ordertimeline',
        'add_purchaseorder',
        'add_purchaseorderdetail',
        'add_supplier',
        'add_suppliermaterial',
        'change_ordertimeline',
        'change_purchaseorder',
        'change_purchaseorderdetail',
        'change_supplier',
        'change_suppliermaterial',
        'handle_purchasing',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Sales': [
        'add_client',
        'add_ordertimeline',
        'add_salesorder',
        'add_salesorderdetail',
        'add_stockallocation',
        'allocate_order_stock',
        'change_client',
        'change_ordertimeline',
        'change_salesorder',
        'change_salesorderdetail',
        'change_stockallocation',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Staff (Editor)': [
        'add_batch',
        'add_client',
        'add_material',
        'add_notification',
        'add_ordertimeline',
        'add_product',
        'add_productionconsumption',
        'add_productionrun',
        'add_productionrunyieldlog',
        'add_productrecipe',
        'add_purchaseorder',
        'add_purchaseorderdetail',
        'add_registrylog',
        'add_runmaterialusage',
        'add_salesorder',
        'add_salesorderdetail',
        'add_shipment',
        'add_shipmentitem',
        'add_shipmentitemreceipt',
        'add_stockallocation',
        'add_stockaudit',
        'add_supplier',
        'add_suppliermaterial',
        'add_warehouse',
        'add_warehouselocation',
        'allocate_order_stock',
        'allocate_run_materials',
        'change_batch',
        'change_client',
        'change_material',
        'change_notification',
        'change_ordertimeline',
        'change_product',
        'change_productionconsumption',
        'change_productionrun',
        'change_productionrunyieldlog',
        'change_productrecipe',
        'change_purchaseorder',
        'change_purchaseorderdetail',
        'change_registrylog',
        'change_runmaterialusage',
        'change_salesorder',
        'change_salesorderdetail',
        'change_shipment',
        'change_shipmentitem',
        'change_shipmentitemreceipt',
        'change_stockallocation',
        'change_stockaudit',
        'change_supplier',
        'change_suppliermaterial',
        'change_warehouse',
        'change_warehouselocation',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Staff (Viewer)': [
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
    'Warehouse': [
        'add_batch',
        'add_registrylog',
        'add_shipmentitemreceipt',
        'add_stockaudit',
        'add_warehouselocation',
        'change_batch',
        'change_registrylog',
        'change_shipmentitemreceipt',
        'change_stockaudit',
        'change_warehouselocation',
        'view_batch',
        'view_client',
        'view_material',
        'view_notification',
        'view_opsbriefing',
        'view_ordertimeline',
        'view_product',
        'view_productionconsumption',
        'view_productionrun',
        'view_productionrunyieldlog',
        'view_productrecipe',
        'view_purchaseorder',
        'view_purchaseorderdetail',
        'view_registrylog',
        'view_runmaterialusage',
        'view_salesorder',
        'view_salesorderdetail',
        'view_shipment',
        'view_shipmentitem',
        'view_shipmentitemreceipt',
        'view_stockallocation',
        'view_stockaudit',
        'view_supplier',
        'view_suppliermaterial',
        'view_warehouse',
        'view_warehouselocation',
        'view_warehouseutilizationsnapshot',
    ],
}


def seed_settings_and_roles(apps, schema_editor):
    SystemSetting = apps.get_model('core', 'SystemSetting')
    for key, (default, value_type, description) in REGISTRY.items():
        SystemSetting.objects.update_or_create(
            key=key, defaults={'value': str(default), 'value_type': value_type, 'description': description})

    # Permissions are normally created after migrate finishes; make them now so they can be granted.
    core_config = apps.get_app_config('core')
    core_config.models_module = True
    create_contenttypes(core_config, verbosity=0, apps=apps)
    create_permissions(core_config, verbosity=0, apps=apps)
    core_config.models_module = None

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    core_perms = {p.codename: p for p in Permission.objects.filter(content_type__app_label='core')}
    for name, codenames in GROUP_PERMS.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.add(*[core_perms[c] for c in codenames if c in core_perms])


class Migration(migrations.Migration):

    initial = True

    replaces = [
        ('core', '0001_initial'),
        ('core', '0002_customuser_branch_and_more'),
        ('core', '0003_material_product_warehouse_notification_batch_and_more'),
        ('core', '0004_batch_allocated_quantity_alter_productionrun_status_and_more'),
        ('core', '0005_remove_shipment_batch_remove_shipment_material_and_more'),
        ('core', '0006_shipment_acknowledged_by_alter_shipment_status'),
        ('core', '0007_shipment_last_edited_by_and_more'),
        ('core', '0008_shipment_approved_by_shipment_assigned_manager_and_more'),
        ('core', '0009_shipmentitem_date_confirmed'),
        ('core', '0010_purchaseorder_approval_remarks_and_more'),
        ('core', '0011_purchaseorder_followers_salesorder_followers'),
        ('core', '0012_purchaseorder_updated_at_purchaseorder_updated_by_and_more'),
        ('core', '0013_role_purchaseorder_linked_production_run_and_more'),
        ('core', '0014_customuser_allowed_locations_customuser_roles_and_more'),
        ('core', '0015_copy_user_roles'),
        ('core', '0016_shipment_linked_production_run'),
        ('core', '0017_alter_shipment_status'),
        ('core', '0018_shipment_assigned_to_shipment_followers'),
        ('core', '0019_ordertimeline_shipment'),
        ('core', '0020_batch_reserved_quantity_productionrun_exact_end_time_and_more'),
        ('core', '0021_material_is_active_ordertimeline_production_run_and_more'),
        ('core', '0022_productionrun_actual_yield'),
        ('core', '0023_shipment_client_address_shipment_client_contact_and_more'),
        ('core', '0024_alter_batch_location'),
        ('core', '0025_batch_warehouse_alter_batch_location'),
        ('core', '0026_productionrun_created_by'),
        ('core', '0027_remove_batch_reserved_quantity'),
        ('core', '0028_widen_ordertimeline_action'),
        ('core', '0029_client_supplier_salesorder_client_and_more'),
        ('core', '0030_alter_salesorder_status_shipmentitemreceipt'),
        ('core', '0031_shipment_client_contact_name_and_more'),
        ('core', '0032_split_client_contact'),
        ('core', '0033_remove_shipment_client_contact'),
        ('core', '0034_alter_productionrun_status_productionrunyieldlog'),
        ('core', '0035_registrylog_material'),
        ('core', '0036_backfill_registrylog_material'),
        ('core', '0037_purchaseorder_completed_date_systemsetting'),
        ('core', '0038_seed_settings_backfill_completed_date'),
        ('core', '0039_seed_so_at_risk_window_setting'),
        ('core', '0040_warehouseutilizationsnapshot'),
        ('core', '0041_opsbriefing'),
        ('core', '0042_seed_ops_briefing_settings'),
        ('core', '0043_opsbriefing_category'),
        ('core', '0044_seed_logistics_settings'),
        ('core', '0045_batch_rental_fields'),
        ('core', '0046_backfill_batch_rental_rate'),
        ('core', '0047_fix_rental_rate_live_tracking'),
        ('core', '0048_product_weight_help_text'),
        ('core', '0049_rescale_products_and_randomize_bom'),
        ('core', '0050_convert_materials_to_kg'),
        ('core', '0051_shipment_credited_to_so'),
        ('core', '0052_normalize_registrylog_quantity_sign'),
        ('core', '0053_shipment_draft_label'),
        ('core', '0054_recipe_quantity_4dp'),
        ('core', '0055_seed_qa_hold_setting'),
        ('core', '0056_warehouse_storage_and_manufacturing'),
        ('core', '0057_snapshot_rent_fields'),
        ('core', '0058_backfill_snapshot_rent'),
        ('core', '0059_capability'),
        ('core', '0060_roles_to_groups'),
        ('core', '0061_remove_legacy_roles'),
        ('core', '0062_seed_dss_saving_settings'),
        ('core', '0063_rentsuggestion'),
        ('core', '0064_seed_dss_snooze_setting'),
        ('core', '0065_productionrun_approved_by'),
        ('core', '0066_runextramaterial'),
        ('core', '0067_registrylog_draft_transfer_deleted'),
        ('core', '0068_contact_validators'),
        ('core', '0069_sales_order_cancel_and_permissions'),
        ('core', '0070_split_approve_requests'),
        ('core', '0071_briefing_tick'),
        ('core', '0072_user_temp_password'),
        ('core', '0073_user_audit_log'),
        ('core', '0074_audit_log_more_actions'),
        ('core', '0075_material_reorder_point'),
        ('core', '0076_merge_purchaser_into_purchasing'),
    ]

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="Capability",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
            ],
            options={
                "permissions": [
                    ("approve_sales_orders", "Can approve sales orders"),
                    ("approve_purchase_orders", "Can approve purchase orders"),
                    ("approve_production_runs", "Can approve production runs"),
                    (
                        "approve_shipments",
                        "Can approve shipments and force-close discrepant ones",
                    ),
                    (
                        "override_approvals",
                        "Can work in every facility and use the system tools",
                    ),
                    ("adjust_physical_stock", "Can manually adjust physical stock"),
                    ("manage_users", "Can manage users and their facility access"),
                    ("set_order_status", "Can set a sales order status by hand"),
                    (
                        "handle_purchasing",
                        "Receives purchase orders drafted for material shortages",
                    ),
                    (
                        "allocate_order_stock",
                        "Can reserve finished goods for sales orders",
                    ),
                    (
                        "allocate_run_materials",
                        "Can reserve materials for production runs",
                    ),
                    (
                        "cancel_orders",
                        "Can cancel sales orders and abort production runs",
                    ),
                ],
                "managed": False,
                "default_permissions": (),
            },
        ),
        migrations.CreateModel(
            name="Client",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                (
                    "contact_person",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("email", models.EmailField(blank=True, max_length=254, null=True)),
                (
                    "phone",
                    models.CharField(
                        blank=True,
                        max_length=50,
                        null=True,
                        validators=[core.validators.validate_phone],
                    ),
                ),
                ("delivery_address", models.TextField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="Material",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("sku", models.CharField(max_length=100, unique=True)),
                ("category", models.CharField(max_length=100)),
                (
                    "unit_of_measure",
                    models.CharField(
                        choices=[
                            ("MT", "Metric Ton"),
                            ("kg", "Kilograms"),
                            ("L", "Litres"),
                            ("g", "Grams"),
                            ("pcs", "Pieces"),
                        ],
                        default="MT",
                        max_length=20,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "safe_storage_days",
                    models.IntegerField(
                        help_text="Days until predictive degradation alert"
                    ),
                ),
                (
                    "weight_mt_per_unit",
                    models.DecimalField(
                        decimal_places=4,
                        default=1.0,
                        help_text="Weight in MT per unit",
                        max_digits=10,
                    ),
                ),
                (
                    "cost_per_unit",
                    models.DecimalField(decimal_places=2, default=0.0, max_digits=12),
                ),
                (
                    "reorder_point",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Reorder when available stock (on hand minus allocated) falls to this level, in the material's own unit. Blank = days-of-cover forecast only.",
                        max_digits=12,
                        null=True,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Product",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("sku", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True, null=True)),
                (
                    "unit_of_measure",
                    models.CharField(
                        choices=[
                            ("MT", "Metric Ton"),
                            ("kg", "Kilograms"),
                            ("L", "Litres"),
                            ("g", "Grams"),
                            ("pcs", "Pieces"),
                        ],
                        default="pcs",
                        max_length=20,
                    ),
                ),
                (
                    "weight_mt_per_unit",
                    models.DecimalField(
                        decimal_places=4,
                        default=1.0,
                        help_text="Weight in MT per unit, regardless of unit_of_measure's label",
                        max_digits=10,
                    ),
                ),
                (
                    "price_per_unit",
                    models.DecimalField(decimal_places=2, default=0.0, max_digits=12),
                ),
                ("is_active", models.BooleanField(default=True)),
            ],
        ),
        migrations.CreateModel(
            name="Supplier",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                (
                    "contact_person",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("email", models.EmailField(blank=True, max_length=254, null=True)),
                (
                    "phone",
                    models.CharField(
                        blank=True,
                        max_length=50,
                        null=True,
                        validators=[core.validators.validate_phone],
                    ),
                ),
                ("address", models.TextField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="Warehouse",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                (
                    "location_type",
                    models.CharField(
                        choices=[
                            ("Storage", "Storage"),
                            ("Manufacturing", "Manufacturing"),
                            ("Both", "Storage & Manufacturing"),
                        ],
                        max_length=50,
                    ),
                ),
                (
                    "ownership_type",
                    models.CharField(
                        choices=[
                            ("Internal", "Internal"),
                            ("ExternalProvider", "Service Provider (External)"),
                            ("SupplierStorage", "Supplier Storage"),
                        ],
                        default="Internal",
                        max_length=50,
                    ),
                ),
                (
                    "rental_billing_method",
                    models.CharField(
                        choices=[
                            ("Usage", "Based on Space Used"),
                            ("Overall", "Fixed on Total Capacity"),
                        ],
                        default="Usage",
                        max_length=20,
                    ),
                ),
                (
                    "rental_cost_per_mt",
                    models.DecimalField(
                        decimal_places=2,
                        default=0.0,
                        help_text="Daily rental cost per Metric Ton",
                        max_digits=10,
                    ),
                ),
                (
                    "total_capacity_mt",
                    models.DecimalField(
                        decimal_places=2,
                        default=1000.0,
                        help_text="Total capacity in Metric Tons",
                        max_digits=12,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="CustomUser",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("password", models.CharField(max_length=128, verbose_name="password")),
                (
                    "last_login",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="last login"
                    ),
                ),
                (
                    "is_superuser",
                    models.BooleanField(
                        default=False,
                        help_text="Designates that this user has all permissions without explicitly assigning them.",
                        verbose_name="superuser status",
                    ),
                ),
                (
                    "username",
                    models.CharField(
                        error_messages={
                            "unique": "A user with that username already exists."
                        },
                        help_text="Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.",
                        max_length=150,
                        unique=True,
                        validators=[
                            django.contrib.auth.validators.UnicodeUsernameValidator()
                        ],
                        verbose_name="username",
                    ),
                ),
                (
                    "first_name",
                    models.CharField(
                        blank=True, max_length=150, verbose_name="first name"
                    ),
                ),
                (
                    "last_name",
                    models.CharField(
                        blank=True, max_length=150, verbose_name="last name"
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        blank=True, max_length=254, verbose_name="email address"
                    ),
                ),
                (
                    "is_staff",
                    models.BooleanField(
                        default=False,
                        help_text="Designates whether the user can log into this admin site.",
                        verbose_name="staff status",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Designates whether this user should be treated as active. Unselect this instead of deleting accounts.",
                        verbose_name="active",
                    ),
                ),
                (
                    "date_joined",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="date joined"
                    ),
                ),
                (
                    "branch",
                    models.CharField(
                        default="HQ",
                        help_text="Department / Division mapping",
                        max_length=100,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("must_change_password", models.BooleanField(default=False)),
                ("temp_password_set_at", models.DateTimeField(blank=True, null=True)),
                (
                    "groups",
                    models.ManyToManyField(
                        blank=True,
                        help_text="The groups this user belongs to. A user will get all permissions granted to each of their groups.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.group",
                        verbose_name="groups",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_users",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user_permissions",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Specific permissions for this user.",
                        related_name="user_set",
                        related_query_name="user",
                        to="auth.permission",
                        verbose_name="user permissions",
                    ),
                ),
                (
                    "allowed_locations",
                    models.ManyToManyField(
                        blank=True, related_name="allowed_users", to="core.warehouse"
                    ),
                ),
            ],
            options={
                "verbose_name": "user",
                "verbose_name_plural": "users",
                "abstract": False,
            },
            managers=[
                ("objects", django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name="Notification",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("message", models.TextField()),
                ("link", models.CharField(blank=True, max_length=255, null=True)),
                ("is_read", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="OpsBriefing",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("generated_at", models.DateTimeField(auto_now_add=True)),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("legacy", "Legacy (pre-category)"),
                            ("materials", "Materials"),
                            ("products", "Products"),
                            ("sales", "Sales"),
                            ("purchase", "Purchase"),
                            ("logistics", "Logistics"),
                            ("warehouse", "Warehouse"),
                            ("my_checklist", "My Checklist"),
                        ],
                        db_index=True,
                        max_length=20,
                    ),
                ),
                (
                    "period",
                    models.CharField(
                        choices=[("daily", "Daily"), ("weekly", "Weekly")],
                        default="daily",
                        max_length=10,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ok", "Generated"),
                            ("empty", "No notable signals"),
                            ("skipped", "Skipped (disabled)"),
                            ("error", "Failed"),
                        ],
                        default="ok",
                        max_length=10,
                    ),
                ),
                ("headline", models.CharField(blank=True, max_length=255)),
                ("body_text", models.TextField(blank=True)),
                ("signals_json", models.JSONField(blank=True, default=dict)),
                ("signal_count", models.PositiveIntegerField(default=0)),
                ("model_id", models.CharField(blank=True, max_length=80)),
                ("input_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("output_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("error_detail", models.CharField(blank=True, max_length=500)),
                (
                    "generated_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="Who triggered a manual run (always set for 'my_checklist' - it is who the checklist is for); null for a scheduled category run.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-generated_at"],
            },
        ),
        migrations.CreateModel(
            name="BriefingTick",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("point_index", models.PositiveSmallIntegerField()),
                ("ticked_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="briefing_ticks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "briefing",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ticks",
                        to="core.opsbriefing",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Batch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("batch_number", models.CharField(max_length=100, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Active", "Active"),
                            ("Quarantined", "Quarantined"),
                            ("Spoiled", "Spoiled / Disposed"),
                            ("Depleted", "Depleted"),
                        ],
                        default="Active",
                        max_length=50,
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=12)),
                ("manufacturing_date", models.DateField()),
                ("expiry_date", models.DateField()),
                (
                    "location",
                    models.CharField(
                        blank=True,
                        help_text="Zone/Aisle (Free Text)",
                        max_length=255,
                        null=True,
                    ),
                ),
                (
                    "allocated_quantity",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                (
                    "rental_rate_per_mt",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Rate charged for this batch's rent (RM/MT/day), locked in at receipt (warehouse's rate at the time, or the PO line's negotiated override). Immune to later changes to Warehouse.rental_cost_per_mt.",
                        max_digits=10,
                        null=True,
                    ),
                ),
                (
                    "closed_date",
                    models.DateField(
                        blank=True,
                        help_text="Date this batch stopped costing rent - fully consumed to zero, or spoiled/disposed. Null while the batch is still open (Active/Quarantined).",
                        null=True,
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.material",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="core.product",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProductionRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("run_number", models.CharField(max_length=100, unique=True)),
                (
                    "expected_yield",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "actual_yield",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=12, null=True
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Pending Approval", "Pending Approval"),
                            ("Pending Allocation", "Pending Allocation"),
                            ("Awaiting Materials", "Awaiting Materials"),
                            ("Planned", "Planned"),
                            ("InProgress", "In Progress"),
                            ("Paused", "Paused"),
                            ("Completed", "Completed"),
                            ("Cancelled", "Cancelled"),
                        ],
                        default="Planned",
                        max_length=50,
                    ),
                ),
                ("start_time", models.DateTimeField(blank=True, null=True)),
                ("end_time", models.DateTimeField(blank=True, null=True)),
                ("fefo_override_reason", models.TextField(blank=True, null=True)),
                ("signoff_reason", models.TextField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("exact_start_time", models.DateTimeField(blank=True, null=True)),
                ("exact_end_time", models.DateTimeField(blank=True, null=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "followers",
                    models.ManyToManyField(
                        blank=True,
                        related_name="following_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "supervisor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "supervisor_signoff",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="signed_off_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target_product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.product"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProductionConsumption",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity_used", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "consumed_batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.batch"
                    ),
                ),
                (
                    "production_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consumptions",
                        to="core.productionrun",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="batch",
            name="produced_in",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="produced_batches",
                to="core.productionrun",
            ),
        ),
        migrations.CreateModel(
            name="ProductionRunYieldLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=12)),
                ("log_date", models.DateField()),
                ("notes", models.CharField(blank=True, max_length=255, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "logged_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "production_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="yield_logs",
                        to="core.productionrun",
                    ),
                ),
            ],
            options={
                "ordering": ["-log_date", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ProductRecipe",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "quantity_required",
                    models.DecimalField(decimal_places=4, max_digits=12),
                ),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.material"
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recipe_items",
                        to="core.product",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PurchaseOrder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("po_number", models.CharField(max_length=100, unique=True)),
                ("supplier_name", models.CharField(max_length=255)),
                ("order_date", models.DateField(auto_now_add=True)),
                ("expected_delivery_date", models.DateField(blank=True, null=True)),
                ("completed_date", models.DateField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Draft", "Draft"),
                            ("Pending Approval", "Pending Approval"),
                            ("Pending", "Pending (Approved)"),
                            ("Partially Received", "Partially Received"),
                            ("Completed", "Completed"),
                            ("Rejected", "Rejected"),
                        ],
                        default="Draft",
                        max_length=50,
                    ),
                ),
                ("approval_remarks", models.TextField(blank=True, null=True)),
                ("rejection_reason", models.TextField(blank=True, null=True)),
                ("revision_count", models.IntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_pos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_pos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_pos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "followers",
                    models.ManyToManyField(
                        blank=True,
                        related_name="followed_pos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "linked_production_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="linked_pos",
                        to="core.productionrun",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_pos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="purchase_orders",
                        to="core.supplier",
                    ),
                ),
                (
                    "target_warehouse",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.warehouse"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="batch",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="received_batches",
                to="core.purchaseorder",
            ),
        ),
        migrations.CreateModel(
            name="PurchaseOrderDetail",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "quantity_ordered",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "quantity_received",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                (
                    "unit_price",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=12, null=True
                    ),
                ),
                (
                    "negotiated_rental_rate_per_mt",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Custom negotiated rental rate (RM/MT/day) for batches received against this line. Leave blank to use the destination warehouse's standard rate at time of receipt.",
                        max_digits=10,
                        null=True,
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.material"
                    ),
                ),
                (
                    "purchase_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="core.purchaseorder",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RunExtraMaterial",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=12)),
                ("reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="extra_usages",
                        to="core.batch",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.material"
                    ),
                ),
                (
                    "production_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="extra_sources",
                        to="core.productionrun",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.CreateModel(
            name="RunMaterialUsage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("expected_qty", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "actual_qty",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                (
                    "variance_pct",
                    models.DecimalField(decimal_places=2, default=0, max_digits=10),
                ),
                ("wastage_reason", models.TextField(blank=True, null=True)),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.material"
                    ),
                ),
                (
                    "production_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="material_usages",
                        to="core.productionrun",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SalesOrder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("so_number", models.CharField(max_length=100, unique=True)),
                ("client_name", models.CharField(max_length=255)),
                ("order_date", models.DateField(auto_now_add=True)),
                ("fulfillment_deadline", models.DateField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Draft", "Draft"),
                            ("Pending Approval", "Pending Approval"),
                            ("Pending", "Pending (Approved)"),
                            (
                                "Awaiting Acknowledgement",
                                "Awaiting Manufacturing Acknowledgement",
                            ),
                            ("In Production", "In Production"),
                            ("Ready to Ship", "Ready to Ship"),
                            ("Partially Shipped", "Partially Shipped"),
                            ("Shipped", "Shipped"),
                            ("Delivered", "Delivered"),
                            ("Rejected", "Rejected"),
                            ("Cancelled", "Cancelled"),
                        ],
                        default="Draft",
                        max_length=50,
                    ),
                ),
                ("approval_remarks", models.TextField(blank=True, null=True)),
                ("rejection_reason", models.TextField(blank=True, null=True)),
                ("cancel_reason", models.TextField(blank=True, default="")),
                ("revision_count", models.IntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_sos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_sos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sales_orders",
                        to="core.client",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_sos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "followers",
                    models.ManyToManyField(
                        blank=True,
                        related_name="followed_sos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_sos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "manufacturing_plant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="manufacturing_sos",
                        to="core.warehouse",
                    ),
                ),
                (
                    "origin_warehouse",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.warehouse"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="productionrun",
            name="sales_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="production_runs",
                to="core.salesorder",
            ),
        ),
        migrations.CreateModel(
            name="SalesOrderDetail",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "quantity_ordered",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "quantity_shipped",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                (
                    "unit_price",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=12, null=True
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.product"
                    ),
                ),
                (
                    "sales_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="core.salesorder",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Shipment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "tracking_number",
                    models.CharField(
                        help_text="Internal Truck Fleet ID or tracking #",
                        max_length=255,
                        unique=True,
                    ),
                ),
                (
                    "direction",
                    models.CharField(
                        choices=[
                            ("Inbound", "Inbound (From Supplier)"),
                            ("Outbound", "Outbound (To Client)"),
                            ("Transfer", "Internal Transfer"),
                        ],
                        default="Inbound",
                        max_length=50,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Draft", "Draft"),
                            ("Logistics Review", "Logistics Review"),
                            ("Pending Approval", "Pending Approval"),
                            ("Preparing", "Approved / Preparing"),
                            ("Dispatched", "Dispatched"),
                            ("Arrived", "Arrived"),
                            ("Completed", "Completed"),
                            ("Delayed", "Delayed"),
                            ("Discrepant", "Discrepant (Shortage)"),
                            ("Cancelled", "Cancelled / Scrapped"),
                        ],
                        default="Draft",
                        max_length=50,
                    ),
                ),
                (
                    "external_origin",
                    models.CharField(
                        blank=True,
                        help_text="For inbound from supplier",
                        max_length=255,
                        null=True,
                    ),
                ),
                (
                    "client_address",
                    models.TextField(
                        blank=True,
                        help_text="Destination address for outbound shipments",
                        null=True,
                    ),
                ),
                (
                    "client_contact_name",
                    models.CharField(
                        blank=True,
                        help_text="Contact person's name",
                        max_length=255,
                        null=True,
                    ),
                ),
                (
                    "client_contact_phone",
                    models.CharField(
                        blank=True,
                        help_text="Contact phone or email",
                        max_length=100,
                        null=True,
                        validators=[core.validators.validate_phone_or_email],
                    ),
                ),
                (
                    "external_tracking_id",
                    models.CharField(
                        blank=True,
                        help_text="Real logistics company tracking ID",
                        max_length=255,
                        null=True,
                    ),
                ),
                ("departure_datetime", models.DateTimeField(blank=True, null=True)),
                ("dispatch_date", models.DateField(blank=True, null=True)),
                ("expected_eta_date", models.DateField(blank=True, null=True)),
                ("actual_arrival_date", models.DateField(blank=True, null=True)),
                ("has_discrepancy", models.BooleanField(default=False)),
                ("discrepancy_remarks", models.TextField(blank=True, null=True)),
                ("is_auto_generated", models.BooleanField(default=False)),
                ("credited_to_so", models.BooleanField(default=False)),
                (
                    "acknowledged_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="acknowledged_shipments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_discrepancies",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_manager",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_discrepancies",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "assigned_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_shipments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "followers",
                    models.ManyToManyField(
                        blank=True,
                        related_name="followed_shipments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "last_edited_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="last_edited_shipments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "linked_production_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="linked_shipments",
                        to="core.productionrun",
                    ),
                ),
                (
                    "purchase_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="shipments",
                        to="core.purchaseorder",
                    ),
                ),
                (
                    "sales_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="shipments",
                        to="core.salesorder",
                    ),
                ),
                (
                    "destination_warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="inbound_shipments",
                        to="core.warehouse",
                    ),
                ),
                (
                    "origin_warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="outbound_shipments",
                        to="core.warehouse",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="OrderTimeline",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("action", models.CharField(max_length=255)),
                ("timestamp", models.DateTimeField(auto_now_add=True)),
                ("notes", models.TextField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "production_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timeline",
                        to="core.productionrun",
                    ),
                ),
                (
                    "purchase_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timeline",
                        to="core.purchaseorder",
                    ),
                ),
                (
                    "sales_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timeline",
                        to="core.salesorder",
                    ),
                ),
                (
                    "shipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timeline",
                        to="core.shipment",
                    ),
                ),
            ],
            options={
                "ordering": ["timestamp"],
            },
        ),
        migrations.CreateModel(
            name="ShipmentItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "quantity",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                (
                    "received_quantity",
                    models.DecimalField(decimal_places=2, default=0, max_digits=12),
                ),
                ("date_confirmed", models.DateTimeField(blank=True, null=True)),
                (
                    "batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="shipment_items",
                        to="core.batch",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="core.material",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="core.product",
                    ),
                ),
                (
                    "shipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="core.shipment",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ShipmentItemReceipt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=12)),
                ("received_date", models.DateField()),
                ("notes", models.CharField(blank=True, max_length=255, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "received_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "shipment_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="receipts",
                        to="core.shipmentitem",
                    ),
                ),
            ],
            options={
                "ordering": ["-received_date", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="StockAllocation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="allocations",
                        to="core.batch",
                    ),
                ),
                (
                    "production_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="allocations",
                        to="core.productionrun",
                    ),
                ),
                (
                    "sales_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="allocations",
                        to="core.salesorder",
                    ),
                ),
                (
                    "shipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="allocations",
                        to="core.shipment",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="StockAudit",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "expected_quantity",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "actual_quantity",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                ("audit_date", models.DateTimeField(auto_now_add=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Pending", "Pending (Duplicate/Conflict)"),
                            ("Resolved", "Resolved (Applied)"),
                        ],
                        default="Pending",
                        max_length=50,
                    ),
                ),
                ("source", models.CharField(default="WebForm", max_length=50)),
                (
                    "auditor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.batch"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SupplierMaterial",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "unit_price",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=12, null=True
                    ),
                ),
                (
                    "lead_time_days",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Typical days from order to delivery",
                        null=True,
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.material"
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="core.supplier"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="supplier",
            name="materials_supplied",
            field=models.ManyToManyField(
                blank=True,
                related_name="suppliers",
                through="core.SupplierMaterial",
                to="core.material",
            ),
        ),
        migrations.CreateModel(
            name="SystemSetting",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.CharField(max_length=100, unique=True)),
                ("value", models.CharField(max_length=255)),
                (
                    "value_type",
                    models.CharField(
                        choices=[
                            ("int", "Integer"),
                            ("float", "Float"),
                            ("str", "String"),
                            ("bool", "Boolean"),
                        ],
                        default="str",
                        max_length=10,
                    ),
                ),
                ("description", models.CharField(blank=True, max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["key"],
            },
        ),
        migrations.CreateModel(
            name="UserAuditLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("target_username", models.CharField(max_length=150)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Account created"),
                            ("password_reset", "Password reset by admin"),
                            ("password_changed", "Password changed"),
                            ("roles_changed", "Roles changed"),
                            ("locations_changed", "Facilities changed"),
                            ("activated", "Account activated"),
                            ("deactivated", "Account deactivated"),
                            ("profile_changed", "Profile changed"),
                            ("unlocked", "Sign-in lockout cleared"),
                            ("signed_out_everywhere", "Signed out everywhere"),
                            ("setting_changed", "System setting changed"),
                        ],
                        max_length=30,
                    ),
                ),
                ("details", models.TextField(blank=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_entries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="RentSuggestion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "decision",
                    models.CharField(
                        choices=[("Accepted", "Accepted"), ("Dismissed", "Dismissed")],
                        db_index=True,
                        max_length=10,
                    ),
                ),
                ("decided_at", models.DateTimeField(auto_now_add=True)),
                ("batch_number", models.CharField(max_length=100)),
                ("item_name", models.CharField(blank=True, max_length=255)),
                ("move_mt", models.DecimalField(decimal_places=3, max_digits=14)),
                ("rate_per_mt", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "est_daily_saving",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                (
                    "est_total_saving",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                ("est_stay_days", models.PositiveIntegerField(default=0)),
                (
                    "dismiss_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("not_worth_it", "Not worth the effort"),
                            ("needed_here", "Stock is needed where it is"),
                            ("destination", "Destination isn't suitable"),
                            ("other", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                ("dismiss_note", models.CharField(blank=True, max_length=255)),
                ("snoozed_until", models.DateField(blank=True, null=True)),
                (
                    "batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rent_suggestions",
                        to="core.batch",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rent_decisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "shipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rent_suggestions",
                        to="core.shipment",
                    ),
                ),
                (
                    "destination_warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rent_suggestions_to",
                        to="core.warehouse",
                    ),
                ),
                (
                    "origin_warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rent_suggestions_from",
                        to="core.warehouse",
                    ),
                ),
            ],
            options={
                "ordering": ["-decided_at"],
            },
        ),
        migrations.CreateModel(
            name="RegistryLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "action_type",
                    models.CharField(
                        choices=[
                            ("Inbound", "Inbound"),
                            ("Outbound", "Outbound"),
                            ("Adjusted", "Adjusted (Audit)"),
                            (
                                "Consumed_For_Manufacturing",
                                "Consumed For Manufacturing",
                            ),
                            ("Produced", "Produced"),
                            ("Spoiled_Disposal", "Spoiled / Disposed"),
                            ("QA_Extension", "Expiry Extended (QA)"),
                            ("Draft_Transfer_Deleted", "Draft Transfer Deleted"),
                        ],
                        max_length=50,
                    ),
                ),
                ("item_name", models.CharField(max_length=255)),
                (
                    "quantity_changed",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                ("timestamp", models.DateTimeField(auto_now_add=True)),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="registry_logs",
                        to="core.material",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="core.warehouse",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="productionrun",
            name="manufacturing_plant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="production_runs",
                to="core.warehouse",
            ),
        ),
        migrations.AddField(
            model_name="batch",
            name="warehouse",
            field=models.ForeignKey(
                blank=True,
                help_text="Facility",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="batches",
                to="core.warehouse",
            ),
        ),
        migrations.CreateModel(
            name="WarehouseLocation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("zone_name", models.CharField(max_length=100)),
                ("aisle", models.CharField(max_length=50)),
                (
                    "warehouse",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="locations",
                        to="core.warehouse",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="WarehouseUtilizationSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("snapshot_date", models.DateField()),
                ("used_mt", models.DecimalField(decimal_places=3, max_digits=14)),
                ("capacity_mt", models.DecimalField(decimal_places=3, max_digits=14)),
                (
                    "utilization_percent",
                    models.DecimalField(decimal_places=2, max_digits=6),
                ),
                (
                    "quarantined_mt",
                    models.DecimalField(decimal_places=3, default=0, max_digits=14),
                ),
                (
                    "daily_rent_cost",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=14, null=True
                    ),
                ),
                ("billing_mode", models.CharField(blank=True, max_length=30)),
                ("rent_estimated", models.BooleanField(default=False)),
                (
                    "warehouse",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="utilization_snapshots",
                        to="core.warehouse",
                    ),
                ),
            ],
            options={
                "ordering": ["-snapshot_date", "warehouse_id"],
            },
        ),
        migrations.AddIndex(
            model_name="opsbriefing",
            index=models.Index(
                fields=["category", "-generated_at"],
                name="core_opsbri_categor_1755f2_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="opsbriefing",
            index=models.Index(
                fields=["category", "generated_by", "-generated_at"],
                name="core_opsbri_categor_8064b0_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="briefingtick",
            constraint=models.UniqueConstraint(
                fields=("briefing", "user", "point_index"), name="unique_briefing_tick"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="suppliermaterial",
            unique_together={("supplier", "material")},
        ),
        migrations.AlterUniqueTogether(
            name="warehouseutilizationsnapshot",
            unique_together={("warehouse", "snapshot_date")},
        ),
        migrations.RunPython(seed_settings_and_roles, migrations.RunPython.noop),
    ]
