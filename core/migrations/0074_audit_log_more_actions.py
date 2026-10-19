from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0073_user_audit_log'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userauditlog',
            name='action',
            field=models.CharField(choices=[('created', 'Account created'), ('password_reset', 'Password reset by admin'), ('password_changed', 'Password changed'), ('roles_changed', 'Roles changed'), ('locations_changed', 'Facilities changed'), ('activated', 'Account activated'), ('deactivated', 'Account deactivated'), ('profile_changed', 'Profile changed'), ('unlocked', 'Sign-in lockout cleared'), ('signed_out_everywhere', 'Signed out everywhere'), ('setting_changed', 'System setting changed')], max_length=30),
        ),
    ]
