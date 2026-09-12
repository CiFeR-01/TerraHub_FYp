from django.db import migrations


def split_client_contact(apps, schema_editor):
    Shipment = apps.get_model('core', 'Shipment')
    for shipment in Shipment.objects.exclude(client_contact__isnull=True).exclude(client_contact=''):
        value = shipment.client_contact.strip()
        if ' - ' in value:
            name, phone = value.split(' - ', 1)
            shipment.client_contact_name = name.strip() or None
            shipment.client_contact_phone = phone.strip() or None
        elif value.replace('+', '').replace(' ', '').replace('-', '').isdigit():
            # Looks like just a phone number, no name was ever recorded.
            shipment.client_contact_phone = value
        else:
            shipment.client_contact_name = value
        shipment.save(update_fields=['client_contact_name', 'client_contact_phone'])


def reverse_split(apps, schema_editor):
    Shipment = apps.get_model('core', 'Shipment')
    for shipment in Shipment.objects.all():
        bits = [b for b in [shipment.client_contact_name, shipment.client_contact_phone] if b]
        shipment.client_contact = ' - '.join(bits) if bits else None
        shipment.save(update_fields=['client_contact'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_shipment_client_contact_name_and_more'),
    ]

    operations = [
        migrations.RunPython(split_client_contact, reverse_split),
    ]
