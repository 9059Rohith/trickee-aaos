# Include this only in the simulator car product on the pinned AOSP branch.
# It changes FakeVehicleHardware's initial values; no vehicle bus is written.
LOCAL_PATH := $(call my-dir)
PRODUCT_COPY_FILES += \
    $(LOCAL_PATH)/TrickeeInitialValues.json:$(TARGET_COPY_OUT_VENDOR)/etc/automotive/vhaloverride/TrickeeInitialValues.json
PRODUCT_VENDOR_PROPERTIES += persist.vendor.vhal_init_value_override=true
