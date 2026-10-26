from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # Keep today's warehouse occupancy/rent snapshots current (replaces the cron)
        import core.signals
        # Count failed sign-ins for the login throttle
        import core.login_throttle

