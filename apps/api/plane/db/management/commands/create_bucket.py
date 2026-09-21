# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import boto3
from botocore.exceptions import ClientError

# Django imports
from django.core.management import BaseCommand
from plane.license.utils.instance_value import get_storage_configuration


class Command(BaseCommand):
    help = "Create the default bucket for the instance"

    def handle(self, *args, **options):
        storage = get_storage_configuration()
        bucket_name = storage["bucket_name"]
        try:
            s3_client = boto3.client(
                "s3",
                endpoint_url=storage["endpoint_url"],
                aws_access_key_id=storage["access_key_id"] or None,
                aws_secret_access_key=storage["secret_access_key"] or None,
                region_name=storage["region_name"],
                config=boto3.session.Config(
                    signature_version=storage["signature_version"],
                    s3={"addressing_style": storage["addressing_style"]},
                ),
            )
            self.stdout.write(self.style.NOTICE("Checking bucket..."))
            # Check if the bucket exists
            s3_client.head_bucket(Bucket=bucket_name)
            # If the bucket exists, print a success message
            self.stdout.write(self.style.SUCCESS(f"Bucket '{bucket_name}' exists."))
            return
        except ClientError as e:
            error_code = int(e.response["Error"]["Code"])
            if error_code == 404:
                # Bucket does not exist, create it
                self.stdout.write(self.style.WARNING(f"Bucket '{bucket_name}' does not exist. Creating bucket..."))
                try:
                    s3_client.create_bucket(Bucket=bucket_name)
                    self.stdout.write(self.style.SUCCESS(f"Bucket '{bucket_name}' created successfully."))

                # Handle the exception if the bucket creation fails
                except ClientError as create_error:
                    self.stdout.write(self.style.ERROR(f"Failed to create bucket: {create_error}"))

            # Handle the exception if access to the bucket is forbidden
            elif error_code == 403:
                # Access to the bucket is forbidden
                self.stdout.write(
                    self.style.ERROR(f"Access to the bucket '{bucket_name}' is forbidden. Check permissions.")
                )
            else:
                # Another ClientError occurred
                self.stdout.write(self.style.ERROR(f"Failed to check bucket: {e}"))
        except Exception as ex:
            # Handle any other exception
            self.stdout.write(self.style.ERROR(f"An error occurred: {ex}"))
