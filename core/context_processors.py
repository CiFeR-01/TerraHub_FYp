from .models import SalesOrder, PurchaseOrder, ProductionRun

def approvals_count(request):
    """
    Context processor to make the total number of pending approvals
    available in all templates for the Action Center badge.
    """
    # Only calculate for authenticated users with Admin or Manager roles
    if request.user.is_authenticated:
        if request.user.is_superuser or (hasattr(request.user, 'role') and request.user.role in ['Admin', 'Manager']):
            # For Sales Orders, we assume 'Draft' needs approval to move to 'Pending' (Approved)
            so_count = SalesOrder.objects.filter(status='Draft').count()
            
            # For Production Runs, 'Pending Approval' needs to move to 'Planned'
            run_count = ProductionRun.objects.filter(status='Pending Approval').count()
            
            # For Purchase Orders, 'Pending' needs to be approved to move to 'Sent' or similar
            # Wait, let's just use 'Pending' for POs
            po_count = PurchaseOrder.objects.filter(status='Pending').count()
            
            total_approvals = so_count + run_count + po_count
            return {'pending_approvals_total': total_approvals}
            
    return {'pending_approvals_total': 0}
