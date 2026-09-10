# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from rest_framework import serializers


class ProjectFeatureSerializer(serializers.Serializer):
    modules = serializers.BooleanField(required=False)
    cycles = serializers.BooleanField(required=False)
    views = serializers.BooleanField(required=False)
    pages = serializers.BooleanField(required=False)
    intakes = serializers.BooleanField(required=False)
    work_item_types = serializers.BooleanField(required=False)
