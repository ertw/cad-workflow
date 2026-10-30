import { commandRequest } from "./commands.js";

export function createFilletTool({ showPreview, restore, applied, editOperation, onBusy }) {
    const element = id => document.getElementById(id);
    const radius = element("fillet-radius"), status = element("fillet-status");
    let context = null, reference = null, preview = null, busy = false, generation = 0;

    function message(value, error = false) {
        status.textContent = value;
        status.dataset.error = String(error);
    }

    function update() {
        const validRadius = radius.validity.valid && Number.isFinite(radius.valueAsNumber) && radius.valueAsNumber > 0;
        element("fillet-preview").disabled = busy || !context?.capability?.supported || !reference || !validRadius;
        element("fillet-apply").disabled = busy || !preview;
        element("fillet-cancel").hidden = !preview;
        element("fillet-cancel").disabled = busy;
        radius.disabled = busy;
        element("fillet-edit").hidden = !context?.capability?.operation;
        element("fillet-edit").disabled = busy;
        const selection = reference ? [reference.component, reference.topology === "face" ? "Face boundary selected" : "Edge selected"].filter(Boolean).join(" · ")
            : context?.capability?.supported ? "Select an edge or face." : context?.capability?.reason || "Open a saved native CAD model.";
        element("fillet-selection").textContent = selection;
    }

    function setBusy(value) {
        busy = value;
        update();
        onBusy(value);
    }

    function discard() {
        if (preview) {
            preview = null;
            restore();
        }
        message("");
        update();
    }

    radius.addEventListener("input", discard);
    element("fillet-cancel").addEventListener("click", discard);
    element("fillet-edit").addEventListener("click", async () => {
        const operation = context?.capability?.operation;
        if (busy || !operation) return;
        setBusy(true);
        try {
            if (await editOperation(operation)) {
                reference = { ...operation.base, ...operation.selection };
                radius.value = operation.radius_mm;
                message("Editing saved fillet.");
            }
        } catch (error) { message(error.message, true); }
        finally { setBusy(false); }
    });
    element("fillet-form").addEventListener("submit", async event => {
        event.preventDefault();
        if (element("fillet-preview").disabled) return;
        const current = generation;
        const payload = { action: "preview", ...context.asset, reference: structuredClone(reference), radius: radius.valueAsNumber };
        setBusy(true);
        message("Creating preview…");
        try {
            const result = await commandRequest("fillet", payload);
            if (current !== generation) return;
            preview = result;
            showPreview(result.model);
            message("Preview ready.");
        } catch (error) {
            if (current === generation) { discard(); message(error.message, true); }
        } finally { setBusy(false); }
    });
    element("fillet-apply").addEventListener("click", async () => {
        if (busy || !preview) return;
        const current = generation;
        const previewId = preview.preview_id;
        setBusy(true);
        message("Checking and saving…");
        try {
            const result = await commandRequest("fillet", { action: "apply", preview_id: previewId });
            if (current !== generation) return;
            preview = null;
            await applied(result);
            message("Fillet saved.");
        } catch (error) {
            if (current === generation) message(error.message, true);
        } finally { setBusy(false); }
    });

    update();
    return {
        get previewing() { return Boolean(preview); },
        get busy() { return busy; },
        setContext(value) {
            generation++;
            context = value;
            reference = null;
            preview = null;
            if (value?.capability?.operation?.radius_mm) radius.value = value.capability.operation.radius_mm;
            message("");
            update();
        },
        select(value) {
            if (busy || preview) return;
            reference = ["edge", "face"].includes(value?.topology) ? value : null;
            message("");
            update();
        },
    };
}
