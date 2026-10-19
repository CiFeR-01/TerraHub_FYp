import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0072_user_temp_password'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserAuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('target_username', models.CharField(max_length=150)),
                ('action', models.CharField(choices=[('created', 'Account created'), ('password_reset', 'Password reset by admin'), ('password_changed', 'Password changed'), ('roles_changed', 'Roles changed'), ('locations_changed', 'Facilities changed'), ('activated', 'Account activated'), ('deactivated', 'Account deactivated'), ('profile_changed', 'Profile changed')], max_length=30)),
                ('details', models.TextField(blank=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('target', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_entries', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
