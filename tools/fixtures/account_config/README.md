# MFAA account option fixtures

These synthetic instances were serialized with the **unmodified**
`MaaInterfaceSelectOptionConverter.cs` from MFAAvalonia `v2.15.2`, the client
version pinned by this branch. The small harness projected the task's
`name`, `entry`, `default_check`, and `option` fields with their upstream
JSON attributes; it did not instantiate Avalonia controls.

The converter emitted `index`, `sub_options`, and the input `data` objects.
They cover an unchecked task with Yes/001, a checked task with No and stale
invalid input, and a half-checked task with Yes/2. There is no user data here.

`mfaa-initial-option.json` was emitted after the same converter deserialized
the PI list `["启用多存档"]`: it has `index: 0`. Consequently our cases must
be ordered **Yes, No** to make a new task default to Yes; setting only
`default_case: "Yes"` with Yes second is insufficient for this client.

This verifies the serialization contract, **not** complete UI acceptance.
Source: [MFAA converter](https://github.com/MaaXYZ/MFAAvalonia/blob/v2.15.2/MFAAvalonia/Helper/Converters/MaaInterfaceSelectOptionConverter.cs).

`mfaa-layered-default.json` and `mfaa-layered-no.json` come from MFAAvalonia
`85e03214` (option default layering), projected from a 2026-11-04 trial
instance. That client stores only values that differ from the interface
default: Yes with account 0 leaves `{"name": "启用多存档", "sub_options":
[{"name": "存档名称"}]}`, while No writes `selected_case` with the aligned
index. The agent resolves unset values from the interface default, as MFAA does.
