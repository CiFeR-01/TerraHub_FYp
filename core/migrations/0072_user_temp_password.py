from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0071_briefing_tick'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='must_change_password',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='customuser',
            name='temp_password_set_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
