/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file in the project root for license information.
 */

import { useForm } from "react-hook-form";
import { Button } from "@makeplane/propel/components/button";
import { Select, SelectContent, SelectItem, SelectList, SelectTrigger } from "@makeplane/propel/components/select";
import type { IFormattedInstanceConfiguration, TInstanceStorageConfigurationKeys } from "@plane/types";
// components
import type { TControllerInputFormField } from "@/components/common/controller-input";
import { ControllerInput } from "@/components/common/controller-input";
import { TOAST_TYPE, setToast } from "@/providers/toast";
// hooks
import { useInstance } from "@/hooks/store";

type IInstanceStorageForm = {
  config: IFormattedInstanceConfiguration;
};

type StorageFormValues = Record<TInstanceStorageConfigurationKeys, string>;

type TStorageProvider = "s3" | "r2";

const STORAGE_PROVIDER_OPTIONS: Record<TStorageProvider, string> = {
  s3: "Amazon S3 or another S3-compatible provider",
  r2: "Cloudflare R2",
};

export function InstanceStorageConfigForm(props: IInstanceStorageForm) {
  const { config } = props;
  const { updateInstanceConfigurations } = useInstance();
  const {
    handleSubmit,
    watch,
    setValue,
    control,
    setError,
    formState: { errors, isSubmitting, isDirty },
  } = useForm<StorageFormValues>({
    defaultValues: {
      STORAGE_PROVIDER: config.STORAGE_PROVIDER || "s3",
      CLOUDFLARE_R2_ACCOUNT_ID: config.CLOUDFLARE_R2_ACCOUNT_ID || "",
      AWS_ACCESS_KEY_ID: config.AWS_ACCESS_KEY_ID || "",
      AWS_SECRET_ACCESS_KEY: config.AWS_SECRET_ACCESS_KEY || "",
      AWS_S3_BUCKET_NAME: config.AWS_S3_BUCKET_NAME || "uploads",
      AWS_S3_ENDPOINT_URL: config.AWS_S3_ENDPOINT_URL || "",
      AWS_S3_REGION_NAME: config.AWS_S3_REGION_NAME || "",
      AWS_S3_ADDRESSING_STYLE: config.AWS_S3_ADDRESSING_STYLE || "auto",
      AWS_S3_SIGNATURE_VERSION: config.AWS_S3_SIGNATURE_VERSION || "s3v4",
      SIGNED_URL_EXPIRATION: config.SIGNED_URL_EXPIRATION || "3600",
    },
  });

  const provider = watch("STORAGE_PROVIDER") as TStorageProvider;
  const formFields: TControllerInputFormField<StorageFormValues>[] = [
    {
      key: "AWS_ACCESS_KEY_ID",
      type: "password",
      label: "Access key ID",
      placeholder: "Access key ID",
      error: Boolean(errors.AWS_ACCESS_KEY_ID),
      description: provider === "s3" ? "Leave both keys blank to use the server credential provider chain." : undefined,
      required: provider === "r2",
    },
    {
      key: "AWS_SECRET_ACCESS_KEY",
      type: "password",
      label: "Secret access key",
      placeholder: "Secret access key",
      error: Boolean(errors.AWS_SECRET_ACCESS_KEY),
      required: provider === "r2",
    },
    {
      key: "AWS_S3_BUCKET_NAME",
      type: "text",
      label: "Bucket name",
      placeholder: "uploads",
      error: Boolean(errors.AWS_S3_BUCKET_NAME),
      required: true,
    },
    {
      key: "AWS_S3_ENDPOINT_URL",
      type: "text",
      label: "Endpoint URL",
      description:
        provider === "r2" ? "Optional. Leave blank to derive the Cloudflare R2 endpoint from Account ID." : undefined,
      placeholder: provider === "r2" ? "https://<account-id>.r2.cloudflarestorage.com" : "https://s3.amazonaws.com",
      error: Boolean(errors.AWS_S3_ENDPOINT_URL),
      required: false,
    },
    {
      key: "AWS_S3_REGION_NAME",
      type: "text",
      label: "Region",
      description:
        provider === "r2" ? "Cloudflare R2 uses auto unless your endpoint requires another region." : undefined,
      placeholder: provider === "r2" ? "auto" : "us-east-1",
      error: Boolean(errors.AWS_S3_REGION_NAME),
      required: false,
    },
    {
      key: "AWS_S3_ADDRESSING_STYLE",
      type: "text",
      label: "Addressing style",
      description: "Use auto for most S3 providers. R2 commonly uses virtual.",
      placeholder: "auto",
      error: Boolean(errors.AWS_S3_ADDRESSING_STYLE),
      required: true,
    },
    {
      key: "AWS_S3_SIGNATURE_VERSION",
      type: "text",
      label: "Signature version",
      placeholder: "s3v4",
      error: Boolean(errors.AWS_S3_SIGNATURE_VERSION),
      required: true,
    },
    {
      key: "SIGNED_URL_EXPIRATION",
      type: "text",
      label: "Signed URL expiration (seconds)",
      placeholder: "3600",
      error: Boolean(errors.SIGNED_URL_EXPIRATION),
      required: true,
    },
  ];

  const onSubmit = async (formData: StorageFormValues) => {
    if (Boolean(formData.AWS_ACCESS_KEY_ID.trim()) !== Boolean(formData.AWS_SECRET_ACCESS_KEY.trim())) {
      setError("AWS_ACCESS_KEY_ID", { message: "Provide both keys or leave both blank." });
      setError("AWS_SECRET_ACCESS_KEY", { message: "Provide both keys or leave both blank." });
      return;
    }
    const payload: Partial<StorageFormValues> = {
      ...formData,
      AWS_S3_REGION_NAME: formData.AWS_S3_REGION_NAME || (provider === "r2" ? "auto" : ""),
    };

    try {
      await updateInstanceConfigurations(payload);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Success",
        message: "Object storage settings updated successfully.",
      });
    } catch (error) {
      console.error("Error updating object storage settings", error);
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Unable to save",
        message: "Check the object storage values and try again.",
      });
    }
  };

  return (
    <div className="space-y-8">
      <div className="space-y-3">
        <div>
          <div className="text-18 font-medium text-primary">Provider</div>
          <div className="text-13 font-regular text-tertiary">
            Configure the S3-compatible storage used for uploads, downloads, and generated exports.
          </div>
        </div>
        <div className="flex max-w-4xl flex-col gap-1">
          <h4 className="text-13 text-tertiary">Storage provider</h4>
          <Select
            value={provider}
            onValueChange={(value) => setValue("STORAGE_PROVIDER", value as TStorageProvider, { shouldDirty: true })}
          >
            <SelectTrigger size="lg" placeholder="Select a storage provider" />
            <SelectContent>
              <SelectList>
                {Object.entries(STORAGE_PROVIDER_OPTIONS).map(([key, label]) => (
                  <SelectItem key={key} value={key} label={label} size="lg" />
                ))}
              </SelectList>
            </SelectContent>
          </Select>
        </div>
      </div>

      {provider === "r2" && (
        <div className="max-w-4xl">
          <ControllerInput
            control={control}
            type="text"
            name="CLOUDFLARE_R2_ACCOUNT_ID"
            label="Cloudflare account ID"
            description="Used to derive the default R2 endpoint when Endpoint URL is blank."
            placeholder="32-character Cloudflare account ID"
            error={Boolean(errors.CLOUDFLARE_R2_ACCOUNT_ID)}
            required
          />
        </div>
      )}

      <div className="grid-col grid w-full max-w-4xl grid-cols-1 items-start justify-between gap-x-12 gap-y-8 lg:grid-cols-2">
        {formFields.map((field) => (
          <ControllerInput
            key={field.key}
            control={control}
            type={field.type}
            name={field.key}
            label={field.label}
            description={field.description}
            placeholder={field.placeholder}
            error={field.error}
            required={field.required}
          />
        ))}
      </div>

      <div className="flex flex-col items-start gap-3">
        <Button
          variant="primary"
          size="md"
          stretch="auto"
          onClick={handleSubmit(onSubmit)}
          loading={isSubmitting}
          disabled={!isDirty}
          label={isSubmitting ? "Saving" : "Save changes"}
        />
        <p className="max-w-4xl text-11 text-tertiary">
          Storage credentials are encrypted before they are persisted. The effective configuration uses the saved values
          when database-backed instance configuration is enabled; otherwise the environment variables remain
          authoritative.
        </p>
      </div>
    </div>
  );
}
