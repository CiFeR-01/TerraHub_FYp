from django.db import models
from django.contrib.auth.models import AbstractUser

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

    @property
    def unread_notifications_count(self):
        try:
            return self.notifications.filter(is_read=False).count()
        except AttributeError:
            return 0
