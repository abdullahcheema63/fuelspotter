from django.core.management.base import BaseCommand
from rest_framework_api_key.models import APIKey


class Command(BaseCommand):
    help = 'Create an API key and print it. The key is shown once and cannot be recovered.'

    def add_arguments(self, parser):
        parser.add_argument('name', help='Who or what the key is for, e.g. "postman".')

    def handle(self, *args, **options):
        _, key = APIKey.objects.create_key(name=options['name'])
        self.stdout.write(self.style.SUCCESS(f"API key for '{options['name']}':"))
        self.stdout.write(key)
        self.stdout.write('Send it as the header:  Authorization: Api-Key ' + key)
