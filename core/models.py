from django.db import models
from django.core.validators import MinValueValidator
from django.contrib.auth.models import AbstractUser
from datetime import date
from django.utils import timezone

from .validators import validate_phone, validate_phone_or_email

class Capability(models.Model):
    """No table: a home for app-wide permissions not tied to one model. Roles are Django Groups; code checks permissions, never role names (see core/permissions.py)."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ('approve_sales_orders', 'Can approve sales orders'),
            ('approve_purchase_orders', 'Can approve purchase orders'),
            ('approve_production_runs', 'Can approve production runs'),
            ('approve_shipments', 'Can approve shipments and force-close discrepant ones'),
            ('override_approvals', 'Can work in every facility and use the system tools'),
            ('adjust_physical_stock', 'Can manually adjust physical stock'),
            ('manage_users', 'Can manage users and their facility access'),
            ('set_order_status', 'Can set a sales order status by hand'),
            ('handle_purchasing', 'Receives purchase orders drafted for material shortages'),
            ('allocate_order_stock', 'Can reserve finished goods for sales orders'),
            ('allocate_run_materials', 'Can reserve materials for production runs'),
            ('cancel_orders', 'Can cancel sales orders and abort production runs'),
            ('request_qa', 'Can send batches to QA'),
            ('perform_qa', 'Can quarantine, inspect, release and extend batches'),
            ('dispose_stock', 'Can write off spoiled stock and return it to suppliers'),
            ('approve_qa_writeoffs', 'Can approve large write-offs and long shelf-life extensions'),
            ('generate_my_checklist', 'Can generate their own AI My Open Jobs checklist'),
        ]


class CustomUser(AbstractUser):
    branch = models.CharField(max_length=100, default='HQ', help_text="Department / Division mapping")

    allowed_locations = models.ManyToManyField('Warehouse', blank=True, related_name='allowed_users')
    updated_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_users')
    updated_at = models.DateTimeField(auto_now=True)
    # Set by an admin password reset (core.account): the user must choose a new password at next sign-in.
    must_change_password = models.BooleanField(default=False)
    temp_password_set_at = models.DateTimeField(null=True, blank=True)

    @property
    def role_label(self):
        """The user's group names for display, e.g. "Manager" or "Sales, Logistics"."""
        names = [g.name for g in self.groups.all()]
        if names:
            return ', '.join(names)
        return 'Superuser' if self.is_superuser else 'No role'

    @property
    def unread_notifications_count(self):
        return self.notifications.filter(is_read=False).count()

class Warehouse(models.Model):
    LOCATION_CHOICES = (
        ('Storage', 'Storage'),
        ('Manufacturing', 'Manufacturing'),
        ('Both', 'Storage & Manufacturing'),
    )
    # Filter with location_type__in=...: a 'Both' facility is a storage place and a plant.
    STORAGE_TYPES = ('Storage', 'Both')
    MANUFACTURING_TYPES = ('Manufacturing', 'Both')
    OWNERSHIP_CHOICES = (
        ('Internal', 'Internal'),
        ('ExternalProvider', 'Service Provider (External)'),
        ('SupplierStorage', 'Supplier Storage'),
    )
    name = models.CharField(max_length=255)
    location_type = models.CharField(max_length=50, choices=LOCATION_CHOICES)
    ownership_type = models.CharField(max_length=50, choices=OWNERSHIP_CHOICES, default='Internal')

    BILLING_CHOICES = (
        ('Usage', 'Based on Space Used'),
        ('Overall', 'Fixed on Total Capacity'),
    )
    rental_billing_method = models.CharField(max_length=20, choices=BILLING_CHOICES, default='Usage')

    rental_cost_per_mt = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Rental cost per Metric Ton per billing term")
    rental_term_days = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text="Days in one billing term. 1 = daily rate; 30 = each lot is charged the rate per MT for every 30 days (or part) it stays.")
    total_capacity_mt = models.DecimalField(max_digits=12, decimal_places=2, default=1000.00, help_text="Total capacity in Metric Tons")

    @property
    def term_label(self):
        return "per day" if self.rental_term_days == 1 else f"per {self.rental_term_days} days"

    def __str__(self):
        return f"{self.name} ({self.ownership_type})"

class WarehouseLocation(models.Model):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name='locations')
    zone_name = models.CharField(max_length=100)
    aisle = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.warehouse.name} - Zone {self.zone_name} Aisle {self.aisle}"

class Material(models.Model):
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=100)
    UNIT_CHOICES = (('MT', 'Metric Ton'), ('kg', 'Kilograms'), ('L', 'Litres'), ('g', 'Grams'), ('pcs', 'Pieces'))
    unit_of_measure = models.CharField(max_length=20, choices=UNIT_CHOICES, default='MT')
    is_active = models.BooleanField(default=True)
    safe_storage_days = models.IntegerField(help_text="Days until predictive degradation alert")
    weight_mt_per_unit = models.DecimalField(max_digits=12, decimal_places=6, default=1, help_text="Weight in MT per unit; set from the unit for MT/kg/g")
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    reorder_point = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Reorder when available stock (on hand minus allocated) falls to this level, "
                  "in the material's own unit. Blank = days-of-cover forecast only."
    )
    inspect_on_receipt = models.BooleanField(
        default=False, help_text="Deliveries of this material start Quarantined until QA inspects and releases them."
    )

    @property
    def weight_kg_per_unit(self):
        """Weight of one unit in kg, as the pcs/L forms ask for it."""
        kg = self.weight_mt_per_unit * 1000
        return kg.to_integral_value() if kg == kg.to_integral_value() else kg.normalize()

    def __str__(self):
        return f"{self.sku} - {self.name}"

class Product(models.Model):
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    UNIT_CHOICES = (('MT', 'Metric Ton'), ('kg', 'Kilograms'), ('L', 'Litres'), ('g', 'Grams'), ('pcs', 'Pieces'))
    unit_of_measure = models.CharField(max_length=20, choices=UNIT_CHOICES, default='pcs')
    weight_mt_per_unit = models.DecimalField(
        max_digits=12, decimal_places=6, default=1,
        help_text="Weight in MT per unit, regardless of unit_of_measure's label"
    )
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    is_active = models.BooleanField(default=True)
    shelf_life_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Days a finished batch stays usable after production. Blank = 365."
    )

    @property
    def weight_kg_per_unit(self):
        """Weight of one unit in kg, as the pcs/L forms ask for it."""
        kg = self.weight_mt_per_unit * 1000
        return kg.to_integral_value() if kg == kg.to_integral_value() else kg.normalize()

    def __str__(self):
        return f"{self.sku} - {self.name}"

class ProductRecipe(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='recipe_items')
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    # 4 dp so trace ingredients (e.g. 0.0005 kg per unit) don't save as 0
    quantity_required = models.DecimalField(max_digits=12, decimal_places=4)

    def __str__(self):
        return f"{self.product.sku} requires {self.quantity_required} of {self.material.sku}"

class Supplier(models.Model):
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True, validators=[validate_phone])
    address = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    materials_supplied = models.ManyToManyField(Material, through='SupplierMaterial', related_name='suppliers', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class SupplierMaterial(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True, help_text="Typical days from order to delivery")

    class Meta:
        unique_together = ('supplier', 'material')

    def __str__(self):
        return f"{self.supplier.name} supplies {self.material.sku}"

class Client(models.Model):
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True, validators=[validate_phone])
    delivery_address = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class ProductionRun(models.Model):
    STATUS_CHOICES = (
        ('Pending Approval', 'Pending Approval'),
        ('Pending Allocation', 'Pending Allocation'),
        ('Awaiting Materials', 'Awaiting Materials'),
        ('Planned', 'Planned'),
        ('InProgress', 'In Progress'),
        ('Paused', 'Paused'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
    )
    run_number = models.CharField(max_length=100, unique=True)
    target_product = models.ForeignKey(Product, on_delete=models.CASCADE)
    expected_yield = models.DecimalField(max_digits=12, decimal_places=2)
    actual_yield = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Planned')
    supervisor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    sales_order = models.ForeignKey('SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='production_runs')
    manufacturing_plant = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='production_runs')
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    
    # Workflow & Approval Fields
    assigned_to = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_runs')
    followers = models.ManyToManyField(CustomUser, related_name='following_runs', blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_runs')
    
    # New MES Tracking Fields
    fefo_override_reason = models.TextField(blank=True, null=True)
    supervisor_signoff = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='signed_off_runs')
    signoff_reason = models.TextField(blank=True, null=True)
    approved_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_runs')
    approved_at = models.DateTimeField(null=True, blank=True)
    exact_start_time = models.DateTimeField(null=True, blank=True)
    exact_end_time = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Run {self.run_number} - {self.target_product.sku}"

class ProductionRunYieldLog(models.Model):
    """One dated finished-goods yield entry against a run - a running record
    for production that spans several sessions, not one number at the end."""
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='yield_logs')
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    log_date = models.DateField()
    logged_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-log_date', '-created_at']

    def __str__(self):
        return f"{self.production_run.run_number} +{self.quantity} on {self.log_date}"

class RunMaterialUsage(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='material_usages')
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    expected_qty = models.DecimalField(max_digits=12, decimal_places=2)
    actual_qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    variance_pct = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    wastage_reason = models.TextField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.expected_qty and self.expected_qty > 0:
            self.variance_pct = ((self.actual_qty - self.expected_qty) / self.expected_qty) * 100
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.production_run.run_number} - {self.material.sku} Usage"

class RunExtraMaterial(models.Model):
    """Material poured beyond what was allocated to a run: from a specific batch (deducted on completion) or, with batch=None, from unrecorded stock (reason required, nothing deducted)."""
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='extra_sources')
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    batch = models.ForeignKey('Batch', on_delete=models.SET_NULL, null=True, blank=True, related_name='extra_usages')
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        src = self.batch.batch_number if self.batch_id else 'batch not in records'
        return f"{self.production_run.run_number}: +{self.quantity} {self.material.sku} ({src})"


class ProductionConsumption(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='consumptions')
    consumed_batch = models.ForeignKey('Batch', on_delete=models.CASCADE)
    quantity_used = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"Run {self.production_run.run_number} consumed {self.quantity_used} of Batch {self.consumed_batch.batch_number}"

class Batch(models.Model):
    STATUS_CHOICES = (
        ('Active', 'Active'),
        ('Quarantined', 'Quarantined'),
        ('Spoiled', 'Spoiled / Disposed'),
        ('Depleted', 'Depleted'),
    )
    batch_number = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Active')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    purchase_order = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='received_batches')
    produced_in = models.ForeignKey(ProductionRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='produced_batches')
    manufacturing_date = models.DateField()
    expiry_date = models.DateField()
    warehouse = models.ForeignKey('Warehouse', on_delete=models.PROTECT, null=True, blank=True, related_name='batches', help_text='Facility')
    location = models.CharField(max_length=255, null=True, blank=True, help_text='Zone/Aisle (Free Text)')
    allocated_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rental_rate_per_mt = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Rate charged for this batch's rent (RM/MT per the warehouse's billing term), locked in at receipt "
                   "(warehouse's rate at the time, or the PO line's negotiated override). "
                   "Immune to later changes to Warehouse.rental_cost_per_mt."
    )
    # Day the lot arrived at its current facility; billing terms count from here. Transfers create a new batch, so they restart it.
    term_start_date = models.DateField(default=date.today)
    closed_date = models.DateField(
        null=True, blank=True,
        help_text="Date this batch stopped costing rent - fully consumed to zero, or "
                   "spoiled/disposed. Null while the batch is still open (Active/Quarantined)."
    )
    # Expiry as first set; shelf-life extensions are capped against it (core/qa.py).
    original_expiry_date = models.DateField(null=True, blank=True)
    extension_days_total = models.PositiveIntegerField(default=0)
    # Set on a batch split off another (part of a lot held or written off by QA).
    parent_batch = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='child_batches')

    def save(self, *args, **kwargs):
        if self.original_expiry_date is None and self.expiry_date:
            self.original_expiry_date = self.expiry_date
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(kwargs['update_fields']) + ['original_expiry_date']
        super().save(*args, **kwargs)

    @property
    def available_quantity(self):
        return self.quantity - self.allocated_quantity

    def rent_term(self, as_of=None):
        """Billing-term position at a Usage-billed rented facility: {'term_no', 'ends_in', 'rate', 'charged'}, else None.
        A term is charged in full once it starts (31 days at 30-day terms = 2 terms)."""
        w = self.warehouse
        if w is None or w.ownership_type == 'Internal' or w.rental_billing_method != 'Usage':
            return None
        end = self.closed_date or as_of or date.today()
        elapsed = max((end - self.term_start_date).days, 0)
        term_no = elapsed // w.rental_term_days + 1
        rate = self.rental_rate_per_mt if self.rental_rate_per_mt is not None else w.rental_cost_per_mt
        return {
            'term_no': term_no,
            'ends_in': term_no * w.rental_term_days - elapsed,
            'rate': rate,
            # ponytail: uses today's MT for every term; per-term opening MT would need quantity history
            'charged': self.total_weight_mt * rate * term_no,
        }

    @property
    def days_until_expiry(self):
        if self.expiry_date:
            delta = self.expiry_date - date.today()
            return delta.days
        return None
    
    @property
    def total_weight_mt(self):
        if self.material:
            return self.quantity * self.material.weight_mt_per_unit
        if self.product:
            return self.quantity * self.product.weight_mt_per_unit
        return 0

    @property
    def available_weight_mt(self):
        if self.material:
            return self.available_quantity * self.material.weight_mt_per_unit
        if self.product:
            return self.available_quantity * self.product.weight_mt_per_unit
        return 0

    def __str__(self):
        item = self.material if self.material else self.product
        return f"Batch {self.batch_number} - {item}"

class PurchaseOrder(models.Model):
    STATUS_CHOICES = (
        ('Draft', 'Draft'),
        ('Pending Approval', 'Pending Approval'),
        ('Pending', 'Pending (Approved)'),
        ('Partially Received', 'Partially Received'),
        ('Completed', 'Completed'),
        ('Rejected', 'Rejected'),
        ('Cancelled', 'Cancelled'),
    )
    po_number = models.CharField(max_length=100, unique=True)
    supplier_name = models.CharField(max_length=255)
    supplier = models.ForeignKey('Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    target_warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    order_date = models.DateField(auto_now_add=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    # Arrived date for the supplier scorecard, stamped by core/utils.py :: apply_po_material_receipt.
    completed_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Draft')
    
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_pos')
    approved_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_pos')
    assigned_to = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_pos')
    approval_remarks = models.TextField(blank=True, null=True)
    rejection_reason = models.TextField(blank=True, null=True)
    revision_count = models.IntegerField(default=0)
    followers = models.ManyToManyField(CustomUser, blank=True, related_name='followed_pos')
    linked_production_run = models.ForeignKey('ProductionRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='linked_pos')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_pos')
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_value(self):
        """Sum of quantity x unit price over lines that have a price (RM)."""
        from decimal import Decimal
        return sum(
            (i.quantity_ordered * i.unit_price for i in self.items.all() if i.unit_price is not None),
            Decimal('0'),
        )

    def __str__(self):
        return f"PO {self.po_number} - {self.supplier_name}"

class PurchaseOrderDetail(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=2)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    negotiated_rental_rate_per_mt = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Custom negotiated rental rate (RM/MT/day) for batches received against "
                   "this line. Leave blank to use the destination warehouse's standard "
                   "rate at time of receipt."
    )

    @property
    def quantity_in_transit(self):
        shipments = self.purchase_order.shipments.filter(material=self.material).exclude(status='Arrived')
        return sum(s.quantity for s in shipments)

    def __str__(self):
        return f"{self.purchase_order.po_number} - {self.material.sku}"

class SalesOrder(models.Model):
    STATUS_CHOICES = (
        ('Draft', 'Draft'),
        ('Pending Approval', 'Pending Approval'),
        ('Pending', 'Pending (Approved)'),
        ('Awaiting Acknowledgement', 'Awaiting Manufacturing Acknowledgement'),
        ('In Production', 'In Production'),
        ('Ready to Ship', 'Ready to Ship'),
        ('Partially Shipped', 'Partially Shipped'),
        ('Shipped', 'Shipped'),
        ('Delivered', 'Delivered'),
        ('Rejected', 'Rejected'),
        ('Cancelled', 'Cancelled'),
    )
    so_number = models.CharField(max_length=100, unique=True)
    client_name = models.CharField(max_length=255)
    client = models.ForeignKey('Client', on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_orders')
    origin_warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    order_date = models.DateField(auto_now_add=True)
    fulfillment_deadline = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Draft')
    
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_sos')
    approved_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_sos')
    assigned_to = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_sos')
    manufacturing_plant = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='manufacturing_sos')
    approval_remarks = models.TextField(blank=True, null=True)
    rejection_reason = models.TextField(blank=True, null=True)
    cancel_reason = models.TextField(blank=True, default='')
    revision_count = models.IntegerField(default=0)
    followers = models.ManyToManyField(CustomUser, blank=True, related_name='followed_sos')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_sos')
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_value(self):
        """Sum of quantity x unit price over lines that have a price (RM)."""
        from decimal import Decimal
        return sum(
            (i.quantity_ordered * i.unit_price for i in self.items.all() if i.unit_price is not None),
            Decimal('0'),
        )

    def __str__(self):
        return f"SO {self.so_number} - {self.client_name}"

class SalesOrderDetail(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=2)
    quantity_shipped = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    @property
    def quantity_in_transit(self):
        shipments = self.sales_order.shipments.filter(product=self.product).exclude(status='Arrived')
        return sum(s.quantity for s in shipments)

    def __str__(self):
        return f"{self.sales_order.so_number} - {self.product.sku}"

class Shipment(models.Model):
    DIRECTION_CHOICES = (
        ('Inbound', 'Inbound (From Supplier)'),
        ('Outbound', 'Outbound (To Client)'),
        ('Transfer', 'Internal Transfer'),
    )
    STATUS_CHOICES = (
        ('Draft', 'Draft'),
        ('Logistics Review', 'Logistics Review'),
        ('Pending Approval', 'Pending Approval'),
        ('Preparing', 'Approved / Preparing'),
        ('Dispatched', 'Dispatched'),
        ('Arrived', 'Arrived'),
        ('Completed', 'Completed'),
        ('Delayed', 'Delayed'),
        ('Discrepant', 'Discrepant (Shortage)'),
        ('Cancelled', 'Cancelled / Scrapped'),
    )
    tracking_number = models.CharField(max_length=255, unique=True, help_text="Internal Truck Fleet ID or tracking #")
    direction = models.CharField(max_length=50, choices=DIRECTION_CHOICES, default='Inbound')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Draft')
    
    purchase_order = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='shipments')
    sales_order = models.ForeignKey('SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='shipments')
    linked_production_run = models.ForeignKey('ProductionRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='linked_shipments')
    
    origin_warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='outbound_shipments')
    destination_warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='inbound_shipments')
    external_origin = models.CharField(max_length=255, null=True, blank=True, help_text="For inbound from supplier")
    
    # Logistics Tracking Details
    client_address = models.TextField(null=True, blank=True, help_text="Destination address for outbound shipments")
    client_contact_name = models.CharField(max_length=255, null=True, blank=True, help_text="Contact person's name")
    client_contact_phone = models.CharField(max_length=100, null=True, blank=True, validators=[validate_phone_or_email], help_text="Contact phone or email")
    external_tracking_id = models.CharField(max_length=255, null=True, blank=True, help_text="Real logistics company tracking ID")
    departure_datetime = models.DateTimeField(null=True, blank=True)
    
    dispatch_date = models.DateField(null=True, blank=True)
    expected_eta_date = models.DateField(null=True, blank=True)
    actual_arrival_date = models.DateField(null=True, blank=True)
    
    acknowledged_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='acknowledged_shipments')
    last_edited_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='last_edited_shipments')
    
    has_discrepancy = models.BooleanField(default=False)
    discrepancy_remarks = models.TextField(blank=True, null=True)
    assigned_manager = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_discrepancies')
    approved_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_discrepancies')

    assigned_to = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_shipments')
    followers = models.ManyToManyField(CustomUser, blank=True, related_name='followed_shipments')
    
    is_auto_generated = models.BooleanField(default=False)

    # Set once the cargo is credited to the SO's quantity_shipped, to avoid double-counting.
    credited_to_so = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.tracking_number} ({self.status})"

class ShipmentItem(models.Model):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='items')
    batch = models.ForeignKey(Batch, on_delete=models.SET_NULL, null=True, blank=True, related_name='shipment_items')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    received_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    date_confirmed = models.DateTimeField(null=True, blank=True)

    @property
    def shortage_quantity(self):
        """Units sent but not received (never negative - an overage is not a shortage)."""
        return max(self.quantity - (self.received_quantity or 0), 0)

    def __str__(self):
        item_name = self.material.sku if self.material else (self.product.sku if self.product else 'Unknown')
        return f"{self.shipment.tracking_number} - {item_name} (Qty: {self.quantity})"

class ShipmentItemReceipt(models.Model):
    """One dated receiving event against a ShipmentItem - a truck can be received
    in multiple rounds. Kept in sync with ShipmentItem.received_quantity."""
    shipment_item = models.ForeignKey(ShipmentItem, on_delete=models.CASCADE, related_name='receipts')
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    received_date = models.DateField()
    received_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_date', '-created_at']

    def __str__(self):
        return f"{self.shipment_item} +{self.quantity} on {self.received_date}"

class StockAudit(models.Model):
    STATUS_CHOICES = (
        ('Pending', 'Pending (Duplicate/Conflict)'),
        ('Resolved', 'Resolved (Applied)'),
        ('Rejected', 'Rejected (Not Applied)'),
    )
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE)
    expected_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    actual_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    audit_date = models.DateTimeField(auto_now_add=True)
    auditor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    source = models.CharField(max_length=50, default='WebForm')
    # Who applied or rejected the count, when, and (for a rejection) why.
    reviewed_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_audits')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')

    @property
    def variance(self):
        return self.actual_quantity - self.expected_quantity

class RegistryLog(models.Model):
    ACTION_CHOICES = (
        ('Inbound', 'Inbound'),
        ('Outbound', 'Outbound'),
        ('Adjusted', 'Adjusted (Audit)'),
        ('Consumed_For_Manufacturing', 'Consumed For Manufacturing'),
        ('Produced', 'Produced'),
        ('Spoiled_Disposal', 'Spoiled / Disposed'),
        ('QA_Extension', 'Expiry Extended (QA)'),
        ('Draft_Transfer_Deleted', 'Draft Transfer Deleted'),
        ('Lost_In_Transit', 'Lost in Transit'),
    )
    action_type = models.CharField(max_length=50, choices=ACTION_CHOICES)
    item_name = models.CharField(max_length=255)
    # Machine-readable key behind the analytics engine; null for non-material movements.
    material = models.ForeignKey(
        Material, on_delete=models.SET_NULL, null=True, blank=True, related_name='registry_logs'
    )
    # Always positive; direction comes from action_type.
    quantity_changed = models.DecimalField(max_digits=12, decimal_places=2)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.action_type} - {self.item_name} at {self.timestamp}"

class OrderTimeline(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, null=True, blank=True, related_name='timeline')
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, null=True, blank=True, related_name='timeline')
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, null=True, blank=True, related_name='timeline')
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, null=True, blank=True, related_name='timeline')
    action = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['timestamp']

class Notification(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='notifications')
    message = models.TextField()
    link = models.CharField(max_length=255, blank=True, null=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

class StockAllocation(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name='allocations')
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='allocations')
    production_run = models.ForeignKey(ProductionRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='allocations')
    shipment = models.ForeignKey(Shipment, on_delete=models.SET_NULL, null=True, blank=True, related_name='allocations')
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Allocated {self.quantity} from {self.batch.batch_number}"


class UserAuditLog(models.Model):
    """History of account and System Settings changes: who did what, to which user, and when.
    Written by core.account.log_user_change / log_setting_change (setting rows have no target user)."""
    ACTION_CHOICES = (
        ('created', 'Account created'),
        ('password_reset', 'Password reset by admin'),
        ('password_changed', 'Password changed'),
        ('roles_changed', 'Roles changed'),
        ('locations_changed', 'Facilities changed'),
        ('activated', 'Account activated'),
        ('deactivated', 'Account deactivated'),
        ('profile_changed', 'Profile changed'),
        ('unlocked', 'Sign-in lockout cleared'),
        ('signed_out_everywhere', 'Signed out everywhere'),
        ('setting_changed', 'System setting changed'),
        ('role_created', 'Role created'),
        ('role_renamed', 'Role renamed'),
        ('role_permissions_changed', 'Role permissions changed'),
        ('role_deleted', 'Role deleted'),
        ('signed_in', 'Signed in'),
        ('sign_in_failed', 'Sign-in failed'),
        ('locked_out', 'Locked out after failed sign-ins'),
        ('item_archived', 'Item archived'),
        ('item_restored', 'Item restored'),
    )
    # The username is copied so the history still reads right if the account is later renamed or deleted.
    target = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_entries')
    target_username = models.CharField(max_length=150)
    actor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    details = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.get_action_display()}: {self.target_username}"


class SystemSetting(models.Model):
    """Key/value store for operational tunables changed without a deploy.
    Read via core.settings_store.get_setting(key), never accessed directly."""
    TYPE_CHOICES = (
        ('int', 'Integer'),
        ('float', 'Float'),
        ('str', 'String'),
        ('bool', 'Boolean'),
    )
    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=255)
    value_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='str')
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return f"{self.key} = {self.value}"


class WarehouseUtilizationSnapshot(models.Model):
    """One row per warehouse per day: end-of-day occupancy and rent. Kept current by core/signals.py; feeds capacity_forecast() and rent_history()."""
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE, related_name='utilization_snapshots'
    )
    snapshot_date = models.DateField()
    used_mt = models.DecimalField(max_digits=14, decimal_places=3)
    capacity_mt = models.DecimalField(max_digits=14, decimal_places=3)
    utilization_percent = models.DecimalField(max_digits=6, decimal_places=2)
    quarantined_mt = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    # NULL = not recorded (e.g. a seeded row) - rent_history() estimates it on read
    daily_rent_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    billing_mode = models.CharField(max_length=30, blank=True)
    # True when daily_rent_cost was estimated (pre-history backfill) rather than recorded
    rent_estimated = models.BooleanField(default=False)

    class Meta:
        unique_together = ('warehouse', 'snapshot_date')
        ordering = ['-snapshot_date', 'warehouse_id']

    def __str__(self):
        return f"{self.warehouse.name} @ {self.snapshot_date}: {self.utilization_percent}%"


class RentSuggestion(models.Model):
    """A decision on a Rent Opportunities suggestion: Accepted (turned into a transfer) or Dismissed (with a reason). Stores the estimate at the time; suggestions themselves are computed live."""
    DECISION_CHOICES = (('Accepted', 'Accepted'), ('Dismissed', 'Dismissed'))
    DISMISS_REASONS = (
        ('not_worth_it', 'Not worth the effort'),
        ('needed_here', 'Stock is needed where it is'),
        ('destination', "Destination isn't suitable"),
        ('other', 'Other'),
    )
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES, db_index=True)
    decided_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='rent_decisions')
    decided_at = models.DateTimeField(auto_now_add=True)

    batch = models.ForeignKey('Batch', on_delete=models.SET_NULL, null=True, blank=True, related_name='rent_suggestions')
    batch_number = models.CharField(max_length=100)
    item_name = models.CharField(max_length=255, blank=True)
    origin_warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='rent_suggestions_from')
    destination_warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='rent_suggestions_to')

    # the estimate when it was decided
    move_mt = models.DecimalField(max_digits=14, decimal_places=3)
    rate_per_mt = models.DecimalField(max_digits=10, decimal_places=2)
    est_daily_saving = models.DecimalField(max_digits=14, decimal_places=2)
    est_total_saving = models.DecimalField(max_digits=14, decimal_places=2)
    est_stay_days = models.PositiveIntegerField(default=0)

    shipment = models.ForeignKey('Shipment', on_delete=models.SET_NULL, null=True, blank=True, related_name='rent_suggestions')
    dismiss_reason = models.CharField(max_length=20, choices=DISMISS_REASONS, blank=True)
    dismiss_note = models.CharField(max_length=255, blank=True)
    # a dismissed batch is left out of the suggestions until this date
    snoozed_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['-decided_at']

    def __str__(self):
        return f"{self.decision}: {self.batch_number} {self.origin_warehouse} -> {self.destination_warehouse}"


class OpsBriefing(models.Model):
    """A stored run of the Tier 3 "AI Copilot" (core/briefing.py) - a company-wide
    category briefing or one person's checklist. See SYSTEM_DOCUMENTATION.md §8.9-§8.11."""
    PERIOD_CHOICES = (('daily', 'Daily'), ('weekly', 'Weekly'))
    STATUS_CHOICES = (
        ('ok', 'Generated'),
        ('empty', 'No notable signals'),
        ('skipped', 'Skipped (disabled)'),
        ('error', 'Failed'),
    )
    # 'legacy' = rows from before this field existed; invisible to every page.
    CATEGORY_CHOICES = (
        ('legacy', 'Legacy (pre-category)'),
        ('materials', 'Materials'),
        ('products', 'Products'),
        ('sales', 'Sales'),
        ('purchase', 'Purchase'),
        ('logistics', 'Logistics'),
        ('warehouse', 'Warehouse'),
        ('my_checklist', 'My Checklist'),
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    period = models.CharField(max_length=10, choices=PERIOD_CHOICES, default='daily')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='ok')
    headline = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    signals_json = models.JSONField(default=dict, blank=True)
    signal_count = models.PositiveIntegerField(default=0)
    model_id = models.CharField(max_length=80, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    error_detail = models.CharField(max_length=500, blank=True)
    generated_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Who triggered a manual run (always set for 'my_checklist' - "
                  "it is who the checklist is for); null for a scheduled category run.",
    )

    class Meta:
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['category', '-generated_at']),
            models.Index(fields=['category', 'generated_by', '-generated_at']),
        ]

    def __str__(self):
        return f"OpsBriefing #{self.pk} ({self.category}/{self.period}, {self.status}) @ {self.generated_at:%Y-%m-%d %H:%M}"


class BriefingTick(models.Model):
    """One user's tick on one bullet (by position) of a briefing. Personal; a new briefing run starts unticked."""
    briefing = models.ForeignKey(OpsBriefing, on_delete=models.CASCADE, related_name='ticks')
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='briefing_ticks')
    point_index = models.PositiveSmallIntegerField()
    ticked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['briefing', 'user', 'point_index'], name='unique_briefing_tick')]



QA_REASON_CHOICES = (
    ('expired', 'Expired'),
    ('near_expiry', 'Close to expiry'),
    ('moisture', 'Moisture / caking'),
    ('pest', 'Pest or insect damage'),
    ('contamination', 'Contamination or foreign matter'),
    ('packaging', 'Packaging damaged'),
    ('temperature', 'Stored at the wrong temperature'),
    ('odour_colour', 'Odour or colour off'),
    ('damaged_in_transit', 'Damaged in transit'),
    ('customer_return', 'Customer return'),
    ('supplier_defect', 'Supplier defect / recall'),
    ('returned_to_supplier', 'Returned to supplier'),
    ('failed_inspection', 'Failed inspection'),
    ('inbound_check', 'Inbound check'),
    ('routine_check', 'Routine check'),
    ('other', 'Other'),
)


class QARequest(models.Model):
    """A batch someone sent to QA ("Send to QA"). QA works it from the Requests tab and records an outcome."""
    STATUS_CHOICES = (('Open', 'Open'), ('In Review', 'In review'), ('Resolved', 'Resolved'), ('Cancelled', 'Cancelled'))
    PRIORITY_CHOICES = (('Low', 'Low'), ('Normal', 'Normal'), ('High', 'High'), ('Urgent', 'Urgent'))
    OUTCOME_CHOICES = (
        ('released', 'Passed and released'), ('no_issue', 'No issue found'), ('held', 'Kept on hold'),
        ('disposed', 'Written off'), ('extended', 'Shelf life extended'),
        ('cancelled', 'Cancelled'),
    )
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name='qa_requests')
    quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Blank = the whole batch")
    reason = models.CharField(max_length=30, choices=QA_REASON_CHOICES)
    note = models.TextField(blank=True, default='')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='Normal')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Open')
    source = models.CharField(max_length=100, blank=True, default='', help_text="Page it was raised from")
    hold_requested = models.BooleanField(default=False)
    requested_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='qa_requests_raised')
    created_at = models.DateTimeField(auto_now_add=True)
    assigned_to = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='qa_requests_assigned')
    resolved_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='qa_requests_resolved')
    resolved_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, blank=True, default='')
    outcome_note = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    @property
    def is_open(self):
        return self.status in ('Open', 'In Review')

    def __str__(self):
        return f"QA request #{self.pk} for {self.batch.batch_number}"


class QAInspection(models.Model):
    """A recorded inspection of a batch: the checklist ticked and the result."""
    RESULT_CHOICES = (('pass', 'Pass'), ('fail', 'Fail'))
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name='inspections')
    request = models.ForeignKey(QARequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='inspections')
    result = models.CharField(max_length=10, choices=RESULT_CHOICES)
    checks = models.JSONField(default=list, blank=True, help_text='[{"check": name, "ok": bool}]')
    moisture_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    inspector = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class QAApproval(models.Model):
    """A write-off or shelf-life extension above the configured limit, waiting in the Action Center."""
    KIND_CHOICES = (('dispose', 'Write-off'), ('extend', 'Shelf-life extension'))
    STATUS_CHOICES = (('Pending', 'Pending'), ('Approved', 'Approved'), ('Rejected', 'Rejected'))
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name='qa_approvals')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    days = models.PositiveIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=30, choices=QA_REASON_CHOICES)
    note = models.TextField(blank=True, default='')
    value_rm = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    request = models.ForeignKey(QARequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='approvals')
    requested_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='qa_approvals_requested')
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    decided_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='qa_approvals_decided')
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['created_at']


class QAEvent(models.Model):
    """One QA action on a batch, with its reason: the batch's QA history and the data behind the spoilage report."""
    ACTION_CHOICES = (
        ('requested', 'Sent to QA'),
        ('quarantined', 'Put on hold'),
        ('auto_hold', 'Held automatically (expired)'),
        ('inspected', 'Inspected'),
        ('released', 'Released'),
        ('disposed', 'Written off'),
        ('disposed_undone', 'Written off (undone)'),
        ('writeoff_undone', 'Write-off undone'),
        ('extended', 'Shelf life extended'),
        ('split', 'Split off a new batch'),
        ('approval_requested', 'Sent for approval'),
        ('approval_rejected', 'Approval rejected'),
        ('expiry_corrected', 'Expiry date corrected'),
        ('expiry_warning', 'Expiry warning sent'),
        ('hold_alert', 'Long hold alert sent'),
    )
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name='qa_events')
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    reason = models.CharField(max_length=30, choices=QA_REASON_CHOICES, blank=True, default='')
    note = models.TextField(blank=True, default='')
    quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    value_rm = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    request = models.ForeignKey(QARequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['action', 'created_at'])]

    def __str__(self):
        return f"{self.get_action_display()} {self.batch.batch_number}"
