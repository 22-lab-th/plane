# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import os
import uuid

# Third party imports
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from urllib.parse import quote

# Module imports
from plane.utils.exception_logger import log_exception
from storages.backends.s3boto3 import S3Boto3Storage


class S3Storage(S3Boto3Storage):
    def url(self, name, parameters=None, expire=None, http_method=None):
        return name

    """S3 storage class to generate presigned URLs for S3 objects"""

    def __init__(self, request=None):
        # Get the AWS credentials and bucket name from the environment
        self.aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
        # Use the AWS_SECRET_ACCESS_KEY environment variable for the secret key
        self.aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        # Use the AWS_S3_BUCKET_NAME environment variable for the bucket name
        self.aws_storage_bucket_name = os.environ.get("AWS_S3_BUCKET_NAME")
        # R2 requires the SigV4 credential scope region to be "auto", and the
        # documented variable is AWS_S3_REGION_NAME (ARCH-001 §3); AWS_REGION is
        # kept as a fallback for existing deployments.
        self.aws_region = os.environ.get("AWS_S3_REGION_NAME") or os.environ.get("AWS_REGION") or "auto"
        # Addressing style and signature version are provider configuration, not
        # code branches (AD-01). The default is botocore's "auto", which keeps
        # hostname-addressed S3-compatible endpoints (local MinIO, custom gateway
        # hosts) on path-style URLs exactly as before this configuration was
        # introduced; an operator enables virtual-host addressing - the R2 posture
        # in ARCH-001 §3 - with AWS_S3_ADDRESSING_STYLE=virtual.
        self.aws_addressing_style = os.environ.get("AWS_S3_ADDRESSING_STYLE") or "auto"
        self.aws_signature_version = os.environ.get("AWS_S3_SIGNATURE_VERSION", "s3v4")
        self.s3_config = boto3.session.Config(
            signature_version=self.aws_signature_version,
            s3={"addressing_style": self.aws_addressing_style},
        )
        # Use the AWS_S3_ENDPOINT_URL environment variable for the endpoint URL
        self.aws_s3_endpoint_url = os.environ.get("AWS_S3_ENDPOINT_URL") or os.environ.get("MINIO_ENDPOINT_URL")
        # Optional browser-accessible MinIO endpoint for deployments where the API
        # and object storage are exposed on different hosts or ports.
        self.minio_public_endpoint_url = os.environ.get("MINIO_PUBLIC_ENDPOINT_URL")
        # Use the SIGNED_URL_EXPIRATION environment variable for the expiration time (default: 3600 seconds)
        self.signed_url_expiration = int(os.environ.get("SIGNED_URL_EXPIRATION", "3600"))

        if os.environ.get("USE_MINIO") == "1":
            # Determine protocol based on environment variable
            if os.environ.get("MINIO_ENDPOINT_SSL") == "1":
                endpoint_protocol = "https"
            else:
                endpoint_protocol = request.scheme if request else "http"
            public_endpoint_url = self.minio_public_endpoint_url or (
                f"{endpoint_protocol}://{request.get_host()}" if request else self.aws_s3_endpoint_url
            )
            internal_endpoint_url = self.aws_s3_endpoint_url if self.minio_public_endpoint_url else public_endpoint_url
            # Use the internal endpoint for server-side object operations.
            self.s3_client = boto3.client(
                "s3",
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region_name=self.aws_region,
                endpoint_url=internal_endpoint_url,
                config=self.s3_config,
            )
            # Sign browser-facing URLs with the externally reachable endpoint.
            self.presign_s3_client = (
                boto3.client(
                    "s3",
                    aws_access_key_id=self.aws_access_key_id,
                    aws_secret_access_key=self.aws_secret_access_key,
                    region_name=self.aws_region,
                    endpoint_url=public_endpoint_url,
                    config=self.s3_config,
                )
                if public_endpoint_url != internal_endpoint_url
                else self.s3_client
            )
        else:
            # Create an S3 client
            self.s3_client = boto3.client(
                "s3",
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region_name=self.aws_region,
                endpoint_url=self.aws_s3_endpoint_url,
                config=self.s3_config,
            )
            self.presign_s3_client = self.s3_client

    def generate_presigned_post(self, object_name, file_type, file_size, expiration=None):
        """Generate a presigned URL to upload an S3 object"""
        if expiration is None:
            expiration = self.signed_url_expiration
        fields = {"Content-Type": file_type}

        conditions = [
            {"bucket": self.aws_storage_bucket_name},
            ["content-length-range", 1, file_size],
            {"Content-Type": file_type},
        ]

        # Add condition for the object name (key)
        if object_name.startswith("${filename}"):
            conditions.append(["starts-with", "$key", object_name[: -len("${filename}")]])
        else:
            fields["key"] = object_name
            conditions.append({"key": object_name})

        # Generate the presigned POST URL
        try:
            # Generate a presigned URL for the S3 object
            response = self.presign_s3_client.generate_presigned_post(
                Bucket=self.aws_storage_bucket_name,
                Key=object_name,
                Fields=fields,
                Conditions=conditions,
                ExpiresIn=expiration,
            )
        # Handle errors
        except ClientError as e:
            print(f"Error generating presigned POST URL: {e}")
            return None

        return response

    def generate_presigned_put(self, object_name, content_type, expires_in=None):
        """Generate a presigned PUT URL for an exact object key and content type.

        R2 does not implement presigned POST form uploads, so project-file
        uploads use a presigned PUT instead. ``ContentType`` is part of the
        signature, so a PUT that declares a different type is rejected by the
        storage provider with ``403 SignatureDoesNotMatch``; the returned
        ``headers`` are what the browser must send verbatim.
        """
        if expires_in is None:
            # Upload URLs are short-lived: the TTL also expires the quota
            # reservation that was taken for this attempt.
            expires_in = self.signed_url_expiration

        try:
            response = self.presign_s3_client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.aws_storage_bucket_name,
                    "Key": str(object_name),
                    "ContentType": content_type,
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
        except ClientError as e:
            log_exception(e)
            return None

        return {
            "url": response,
            "method": "PUT",
            "headers": {"Content-Type": content_type},
            "expires_in": expires_in,
        }

    def _get_content_disposition(self, disposition, filename=None):
        """Helper method to generate Content-Disposition header value"""
        if filename is None:
            filename = uuid.uuid4().hex

        if filename:
            # Encode the filename to handle special characters
            encoded_filename = quote(filename)
            return f"{disposition}; filename*=UTF-8''{encoded_filename}"
        return disposition

    def generate_presigned_url(
        self,
        object_name,
        expiration=None,
        http_method="GET",
        disposition="inline",
        filename=None,
        response_content_type=None,
    ):
        """Generate a presigned URL to share an S3 object

        ``response_content_type`` (optional) pins the ``Content-Type`` the provider
        must serve, signed into the URL. Project-file delivery uses it so a
        mislabelled object cannot be rendered as a type the server did not
        verify (RSCH-002 T3); a presigned response cannot carry
        ``X-Content-Type-Options``, so pinning the type is the control available.
        """
        if expiration is None:
            expiration = self.signed_url_expiration
        content_disposition = self._get_content_disposition(disposition, filename)
        params = {
            "Bucket": self.aws_storage_bucket_name,
            "Key": str(object_name),
            "ResponseContentDisposition": content_disposition,
        }
        if response_content_type:
            params["ResponseContentType"] = response_content_type

        try:
            response = self.presign_s3_client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expiration,
                HttpMethod=http_method,
            )
        except ClientError as e:
            log_exception(e)
            return None

        # The response contains the presigned URL
        return response

    def get_object_head_bytes(self, object_name, length=512):
        """Read the first ``length`` bytes of an object with one ranged GET.

        This is the magic-byte evidence for finalize (ARCH-001 §5.1): it is one
        Class B operation and never reads the whole object. Returns ``None`` when
        the object cannot be read, so the caller fails closed instead of
        activating an object it could not verify (AD-05).
        """
        try:
            response = self.s3_client.get_object(
                Bucket=self.aws_storage_bucket_name,
                Key=str(object_name),
                Range=f"bytes=0-{max(int(length), 1) - 1}",
            )
        except ClientError as e:
            log_exception(e)
            return None

        body = response.get("Body")
        if body is None:
            return None

        try:
            return body.read(length)
        except Exception as e:  # pragma: no cover - stream failures are provider errors
            log_exception(e)
            return None
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()

    def get_object_metadata(self, object_name):
        """Get the metadata for an S3 object"""
        try:
            response = self.s3_client.head_object(Bucket=self.aws_storage_bucket_name, Key=object_name)
        except ClientError as e:
            log_exception(e)
            return None

        return {
            "ContentType": response.get("ContentType"),
            "ContentLength": response.get("ContentLength"),
            "LastModified": (response.get("LastModified").isoformat() if response.get("LastModified") else None),
            "ETag": response.get("ETag"),
            "Metadata": response.get("Metadata", {}),
        }

    def copy_object(self, object_name, new_object_name):
        """Copy an S3 object to a new location"""
        try:
            response = self.s3_client.copy_object(
                Bucket=self.aws_storage_bucket_name,
                CopySource={"Bucket": self.aws_storage_bucket_name, "Key": object_name},
                Key=new_object_name,
            )
        except ClientError as e:
            log_exception(e)
            return None

        return response

    def upload_file(
        self,
        file_obj,
        object_name: str,
        content_type: str = None,
        extra_args: dict = {},
    ) -> bool:
        """Upload a file directly to S3"""
        try:
            if content_type:
                extra_args["ContentType"] = content_type

            self.s3_client.upload_fileobj(
                file_obj,
                self.aws_storage_bucket_name,
                object_name,
                ExtraArgs=extra_args,
            )
            return True
        except ClientError as e:
            log_exception(e)
            return False

    #: Field names a provider's ``UsageSummary`` response may carry its byte total
    #: under. RSCH-001 S15 confirms the operation exists and is a Class B call, but
    #: not the field names, and the deferred R2 verification covers that gap; the
    #: extractor therefore reads the first shape it recognises and returns ``None``
    #: rather than guessing a number.
    BUCKET_USAGE_FIELDS = ("PayloadSizeBytes", "Bytes", "UsageBytes", "Value", "TotalSize")

    def get_bucket_usage_bytes(self):
        """Return the bucket's own usage in bytes, or ``None`` when unavailable.

        A Class B ``UsageSummary`` call where the provider exposes one (RSCH-001
        S15). ``None`` means "this provider cannot answer" - MinIO in this
        environment, or a provider whose response shape this deployment has not
        verified - and the reconciliation reports it as unavailable instead of
        treating it as zero, which would look like a clean bucket.
        """
        try:
            summary = self.s3_client.get_bucket_usage(Bucket=self.aws_storage_bucket_name)
        except (ClientError, BotoCoreError, AttributeError, TypeError) as e:
            log_exception(e)
            return None

        if not isinstance(summary, dict):
            return None

        for field in self.BUCKET_USAGE_FIELDS:
            value = summary.get(field)
            if isinstance(value, int):
                return value

        for nested in ("Usage", "Summary", "Storage"):
            inner = summary.get(nested)
            if isinstance(inner, dict):
                for field in self.BUCKET_USAGE_FIELDS:
                    value = inner.get(field)
                    if isinstance(value, int):
                        return value

        return None

    def delete_files(self, object_names):
        """Delete an S3 object"""
        try:
            self.s3_client.delete_objects(
                Bucket=self.aws_storage_bucket_name,
                Delete={"Objects": [{"Key": object_name} for object_name in object_names]},
            )
            return True
        except ClientError as e:
            log_exception(e)
            return False
