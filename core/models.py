from django.db import models
from django.contrib.auth.models import AbstractUser
from datetime import date

class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return self.name

class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('Admin', 'Admin'),
        ('Manager', 'Manager'),
        ('Staff_Edit', 'Staff (Editor)'),
        ('Staff_View', 'Staff (Viewer)'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='Staff_View')
    branch = models.CharField(max_length=100, default='HQ', help_text="Department / Division mapping")
    can_adjust_physical_stock = models.BooleanField(default=False, help_text="Explicit permission to adjust warehouse stock")

    roles = models.ManyToManyField(Role, blank=True, related_name='users')
    allowed_locations = models.ManyToManyField('Warehouse', blank=True, related_name='allowed_users')
    updated_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_users')
    updated_at = models.DateTimeField(auto_now=True)
    
    def has_role(self, role_name):
        return self.roles.filter(name=role_name).exists()

    @property
    def unread_notifications_count(self):
        return self.notifications.filter(is_read=False).count()

class Warehouse(models.Model):
    LOCATION_CHOICES = (
        ('Storage', 'Storage'),
        ('Manufacturing', 'Manufacturing'),
    )
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

    rental_cost_per_mt = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Daily rental cost per Metric Ton")
    total_capacity_mt = models.DecimalField(max_digits=12, decimal_places=2, default=1000.00, help_text="Total capacity in Metric Tons")

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
    weight_mt_per_unit = models.DecimalField(max_digits=10, decimal_places=4, default=1.0000, help_text="Weight in MT per unit")
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def __str__(self):
        return f"{self.sku} - {self.name}"

class Product(models.Model):
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    UNIT_CHOICES = (('MT', 'Metric Ton'), ('kg', 'Kilograms'), ('L', 'Litres'), ('g', 'Grams'), ('pcs', 'Pieces'))
    unit_of_measure = models.CharField(max_length=20, choices=UNIT_CHOICES, default='pcs')
    weight_mt_per_unit = models.DecimalField(max_digits=10, decimal_places=4, default=1.0000)
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.sku} - {self.name}"

class ProductRecipe(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='recipe_items')
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    quantity_required = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.product.sku} requires {self.quantity_required} of {self.material.sku}"

class ProductionRun(models.Model):
    STATUS_CHOICES = (
        ('Pending Approval', 'Pending Approval'),
        ('Pending Allocation', 'Pending Allocation'),
        ('Awaiting Materials', 'Awaiting Materials'),
        ('Planned', 'Planned'),
        ('InProgress', 'In Progress'),
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
    
    # New MES Tracking Fields
    fefo_override_reason = models.TextField(blank=True, null=True)
    supervisor_signoff = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='signed_off_runs')
    signoff_reason = models.TextField(blank=True, null=True)
    exact_start_time = models.DateTimeField(null=True, blank=True)
    exact_end_time = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Run {self.run_number} - {self.target_product.sku}"

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
    location = models.ForeignKey(WarehouseLocation, on_delete=models.SET_NULL, null=True, blank=True)
    allocated_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reserved_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    @property
    def available_quantity(self):
        return self.quantity - self.allocated_quantity - self.reserved_quantity

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
    )
    po_number = models.CharField(max_length=100, unique=True)
    supplier_name = models.CharField(max_length=255)
    target_warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    order_date = models.DateField(auto_now_add=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
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

    def __str__(self):
        return f"PO {self.po_number} - {self.supplier_name}"

class PurchaseOrderDetail(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=2)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

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
        ('Shipped', 'Shipped'),
        ('Delivered', 'Delivered'),
        ('Rejected', 'Rejected'),
    )
    so_number = models.CharField(max_length=100, unique=True)
    client_name = models.CharField(max_length=255)
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
    revision_count = models.IntegerField(default=0)
    followers = models.ManyToManyField(CustomUser, blank=True, related_name='followed_sos')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_sos')
    updated_at = models.DateTimeField(auto_now=True)

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
        ('Draft', 'Draft (Manufacturing)'),
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

    def __str__(self):
        item_name = self.material.sku if self.material else (self.product.sku if self.product else 'Unknown')
        return f"{self.shipment.tracking_number} - {item_name} (Qty: {self.quantity})"

class StockAudit(models.Model):
    STATUS_CHOICES = (
        ('Pending', 'Pending (Duplicate/Conflict)'),
        ('Resolved', 'Resolved (Applied)'),
    )
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE)
    expected_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    actual_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    audit_date = models.DateTimeField(auto_now_add=True)
    auditor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    source = models.CharField(max_length=50, default='WebForm')

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
    )
    action_type = models.CharField(max_length=50, choices=ACTION_CHOICES)
    item_name = models.CharField(max_length=255)
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
    action = models.CharField(max_length=100)
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

