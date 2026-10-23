import { Viewer, Display, Timer } from "/static/js/three-cad-viewer.esm.js";
import { createPage } from "/static/js/ocp-viewer-core/index.js";
import { Comms } from "/static/js/comms.js";

const element = (id) => document.getElementById(id);
const surface = element("cad_viewer");
const buildPattern = /^(\d{8}T\d{12}Z-[a-f0-9]{12})(?: cutaway)?$/;
let activeBuild = null;
let parts = new Map();
let selectedPaths = [];
let modelPaths = [];
let request = null;
let revision = 0;
let rendering = false;
let cadViewer = null;
let viewerPage = null;
let catalogMode = false;
let projects = [];
let currentProject = null;
let currentFile = null;
let activeAsset = null;
let sceneRequest = null;
let sceneVersion = 0;
let catalogRequest = null;
let catalogVersion = 0;
let retryScene = null;
let graphicsLost = false;
let geometryReference = null;
let referenceKey = null;
let referenceHighlight = null;
let agentBusy = false;
let buildBusy = false;
let buildOptionsKey = null;
let buildOptionsVersion = 0;
let modelUnits = null;
const safeGraphics = new URLSearchParams(location.search).get("graphics") === "safe"
    || (/AppleWebKit/.test(navigator.userAgent) && !/Chrome|Chromium|Edg\/|OPR\/|Android/.test(navigator.userAgent));

function text(id, value) {
    element(id).textContent = value;
}

function optionalText(id, value) {
    text(id, value || "");
    element(id).hidden = !value;
}

function visitShapes(shape, callback) {
    if (!shape || typeof shape !== "object") return;
    callback(shape);
    for (const child of shape.parts || []) visitShapes(child, callback);
}

// Snapshot only serializable data: upstream picked objects retain the renderer.
function captureReference(picked) {
    if (rendering || surface.dataset.modelReady !== "true") return;
    if (!picked?.backendId) return;
    const camera = cadViewer.getCameraLocationSettings();
    const key = JSON.stringify([picked.backendId, camera]);
    if (key === referenceKey) return;
    referenceKey = key;
    const geometry = cadViewer.rendered.nestedGroup.meshGeometry.resolve(picked.backendId);
    const positions = geometry?.positions;
    let bounds = null;
    if (positions?.length >= 3) {
        const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
        for (let i = 0; i < positions.length; i++) {
            min[i % 3] = Math.min(min[i % 3], positions[i]);
            max[i % 3] = Math.max(max[i % 3], positions[i]);
        }
        bounds = { min, max };
    }
    geometryReference = {
        schema: "cad-geometry-reference/v1",
        viewer_url: location.href,
        project: activeAsset?.project || null,
        file: activeAsset?.file || null,
        revision: activeAsset?.revision || activeBuild,
        build_id: activeBuild,
        units: modelUnits || "unspecified model units",
        geometry_path: picked.backendId,
        component: owner(picked.backendId),
        topology: picked.asSolid ? "solid" : picked.topo,
        geometry_type: geometry?.geomType ?? null,
        selection_point: picked.point?.toArray?.() || null,
        world_bounds: bounds,
        edge_endpoints: picked.topo === "edge" && !picked.asSolid && positions?.length >= 6
            ? [Array.from(positions.slice(0, 3)), Array.from(positions.slice(-3))] : null,
        camera,
    };
    element("reference-text").hidden = true;
    element("copy-reference").disabled = false;
    updateAgentButton();
    text("reference-status", `Captured ${geometryReference.topology}: ${picked.backendId}`);
}

async function copyReference() {
    if (!geometryReference) return;
    const capturedRevision = revision;
    const value = "CAD geometry reference\n" + JSON.stringify(geometryReference, null, 2)
        + "\nGeometry paths identify this displayed revision only; they may change after rebuilding. "
        + "Coordinates and measurements describe the displayed tessellation (including view transforms), not exact B-rep geometry. "
        + "The viewer URL opens the file but does not restore this selection or guarantee the same revision.\n"
        + "Requested change: [describe what should improve and what must stay unchanged]";
    const fallback = element("reference-text");
    fallback.value = value;
    try {
        await navigator.clipboard.writeText(value);
        if (revision !== capturedRevision) return;
        fallback.hidden = true;
        text("reference-status", "Reference copied. Paste it into your agent chat and describe the change.");
    } catch {
        if (revision !== capturedRevision) return;
        fallback.hidden = false;
        fallback.focus();
        fallback.select();
        text("reference-status", "Clipboard unavailable. Copy the selected reference below.");
    }
}

element("copy-reference").addEventListener("click", copyReference);
window.addEventListener("keydown", (event) => {
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.code !== "KeyC"
        || event.repeat || event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    if (!geometryReference || surface.dataset.modelReady !== "true") return;
    event.preventDefault();
    void copyReference();
});

function displayedBuild(message) {
    const ids = new Set();
    const add = (name) => {
        if (typeof name !== "string") return;
        for (const segment of name.split("/")) {
            const match = segment.match(buildPattern);
            if (match) ids.add(match[1]);
        }
    };
    add(message.build_id);
    visitShapes(message.data?.shapes, (shape) => {
        add(shape.name);
        add(shape.id);
    });
    // A mixed-build scene has no unambiguous set of printable exports.
    return ids.size === 1 ? [...ids][0] : null;
}

function owner(path) {
    if (typeof path !== "string") return null;
    // Match complete path segments: body and body_gasket must stay distinct.
    // Face/edge/solid suffixes then resolve to their containing component.
    return path.split("/").reverse().find((segment) => parts.has(segment)) || null;
}

function downloadURL(part, format) {
    const value = part.downloads?.[format];
    if (typeof value !== "string" || !activeBuild) return null;
    try {
        const url = new URL(value, location.origin);
        const assetPrefix = activeAsset
            ? `/api/projects/${encodeURIComponent(activeAsset.project)}/files/${encodeURIComponent(activeAsset.file)}/downloads/${encodeURIComponent(activeAsset.revision)}/`
            : null;
        const matches = url.pathname.startsWith(`/downloads/${encodeURIComponent(activeBuild)}/`)
            || (assetPrefix && url.pathname.startsWith(assetPrefix));
        return url.origin === location.origin && matches ? url.href : null;
    } catch { return null; }
}

function setDownload(format, part) {
    const link = element(`download-${format}`);
    const href = part ? downloadURL(part, format) : null;
    link.hidden = !href;
    link.removeAttribute("href");
    link.removeAttribute("download");
    if (href) {
        link.href = href;
        const filename = part.label || part.name;
        link.download = filename.toLocaleLowerCase().endsWith(`.${format}`) ? filename : `${filename}.${format}`;
    }
    return Boolean(href);
}

function selectPart(name, message = "Choose a component in the model or the parts list.") {
    const part = parts.get(name);
    text("selected-component", part?.label || part?.name || "Select a component");
    optionalText("print-note", part?.print_note);
    const dimensions = part?.print_bbox_mm?.size || part?.print_bbox_mm;
    optionalText("print-dimensions", Array.isArray(dimensions) && dimensions.length === 3
        ? `Print size: ${dimensions.map((value) => Number(value).toFixed(1)).join(" × ")} mm`
        : null);
    const has3mf = setDownload("3mf", part);
    const hasStl = setDownload("stl", part);
    element("download-note").hidden = !(has3mf || hasStl);
    text("download-note", activeAsset?.kind === "stl"
        ? "Millimeters · full source file" : "Print orientation · millimeters · full component");
    element("clear-selection").hidden = !part;
    if (part) {
        text("print-status", has3mf
            ? "Ready to open in your slicer."
            : hasStl ? (catalogMode ? "STL file ready to open in your slicer." : "This build has an STL export. Rebuild to add 3MF.")
                : "No print file is available for this component in the displayed build.");
    } else {
        text("print-status", message);
    }
    for (const button of element("parts-list").querySelectorAll("button")) {
        button.setAttribute("aria-pressed", String(button.dataset.component === part?.name));
    }
}

function applySelection(paths) {
    selectedPaths = paths.filter((path) => typeof path === "string");
    if (!parts.size) return;
    const names = [...new Set(selectedPaths.map(owner).filter(Boolean))];
    selectPart(names.length === 1 ? names[0] : null,
        names.length > 1 ? "Several components are selected. Choose one part below to download."
            : "Choose a component in the model or the parts list.");
}

function onViewerStatus(message) {
    if (rendering) return;
    if (Object.hasOwn(message, "lastPick")) {
        const pick = message.lastPick;
        applySelection(pick ? [`${pick.path}/${pick.name}`] : []);
    } else if (Object.hasOwn(message, "selectedShapeIDs")) {
        applySelection(Array.isArray(message.selectedShapeIDs) ? message.selectedShapeIDs : []);
    }
    // Select-tool status contains only deduplicated face numbers; its actual
    // selected objects are synchronized by EmbeddedViewer's notify hook.
}

function renderParts() {
    const list = element("parts-list");
    list.replaceChildren();
    text("parts-count", String(parts.size));
    for (const part of parts.values()) {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "part-button";
        button.dataset.component = part.name;
        button.setAttribute("aria-pressed", "false");
        const label = document.createElement("span");
        const name = document.createElement("span");
        name.className = "part-name";
        name.textContent = part.label || part.name;
        const kind = document.createElement("span");
        kind.className = "part-kind";
        kind.textContent = [part.kind, part.in_assembly === false ? "fit coupon" : null].filter(Boolean).join(" · ");
        label.append(name, kind);
        const format = document.createElement("span");
        format.className = "part-format";
        format.textContent = downloadURL(part, "3mf") ? "3MF" : downloadURL(part, "stl") ? "STL" : "—";
        button.append(label, format);
        button.addEventListener("click", () => {
            const path = modelPaths.find((path) => owner(path) === part.name);
            if (path && cadViewer?.ready) {
                const slash = path.lastIndexOf("/");
                cadViewer.handlePick(path.slice(0, slash), path.slice(slash + 1), false, false, false, null);
            }
            selectedPaths = [part.name];
            selectPart(part.name);
        });
        item.append(button);
        list.append(item);
    }
}

async function loadPrintFiles(build, expectedRevision) {
    request?.abort();
    request = new AbortController();
    element("retry-downloads").hidden = true;
    text("print-status", "Loading print files for the displayed build…");
    try {
        const response = await fetch(`/api/builds/${encodeURIComponent(build)}`, { signal: request.signal });
        if (!response.ok) throw new Error(`Print files are unavailable (HTTP ${response.status}).`);
        const metadata = await response.json();
        if (expectedRevision !== revision) return;
        if (metadata.build_id !== build || !Array.isArray(metadata.parts)) {
            throw new Error("The print files do not match the displayed build.");
        }
        modelUnits = metadata.units || modelUnits;
        parts = new Map(metadata.parts.map((part) => [part.name, part]));
        renderParts();
        optionalText("build-status", metadata.status);
        if (parts.size) applySelection(selectedPaths);
        else text("print-status", "No printable components were exported for this build.");
    } catch (error) {
        if (error.name === "AbortError" || expectedRevision !== revision) return;
        selectPart(null, `${error.message} The model can still be explored.`);
        element("retry-downloads").hidden = false;
    }
}

function resetModel(message, metadata = null) {
    revision += 1;
    geometryReference = null;
    referenceKey = null;
    referenceHighlight = null;
    updateAgentButton();
    modelUnits = metadata?.units || message?.data?.units || null;
    element("copy-reference").disabled = true;
    element("reference-text").hidden = true;
    element("reference-text").value = "";
    text("reference-status", "Click an edge or face to capture a reference.");
    request?.abort();
    parts = new Map();
    selectedPaths = [];
    modelPaths = [];
    visitShapes(message?.data?.shapes, (shape) => {
        if (typeof shape.id === "string") modelPaths.push(shape.id);
    });
    activeBuild = metadata ? metadata.build_id || metadata.revision : message ? displayedBuild(message) : null;
    surface.dataset.buildId = activeBuild || "";
    surface.dataset.modelReady = "false";
    surface.dataset.projectId = activeAsset?.project || "";
    surface.dataset.fileId = activeAsset?.file || "";
    element("parts-list").replaceChildren();
    text("parts-count", "");
    text("build-id", activeBuild || "No build attached");
    optionalText("build-status", null);
    element("retry-downloads").hidden = true;
    selectPart(null, message
        ? "This model has no Cyclone build attached. Print files appear when a saved build is loaded."
        : "Waiting for a model from the CAD workspace.");
    if (metadata) {
        modelUnits = metadata.units || modelUnits;
        parts = new Map(metadata.parts.map((part) => [part.name, part]));
        renderParts();
        optionalText("build-status", metadata.status);
        if (parts.size === 1) selectPart(parts.keys().next().value);
        else if (parts.size) applySelection([]);
        else selectPart(null, "No print files are available for this file.");
    } else if (activeBuild) void loadPrintFiles(activeBuild, revision);
}

function sceneState(kind, title, detail = "", retry = null) {
    const state = element("scene-state");
    state.hidden = !kind;
    state.dataset.state = kind || "";
    surface.setAttribute("aria-busy", String(kind === "loading"));
    text("scene-state-title", title || "");
    text("scene-state-detail", detail);
    retryScene = retry;
    element("retry-scene").hidden = !retry;
}

function clearPreview() {
    sceneVersion += 1;
    sceneRequest?.abort();
    activeAsset = null;
    rendering = true;
    try {
        resetModel(null);
        viewerPage.handleMessage({ type: "clear" });
    } finally {
        rendering = false;
    }
    optionalText("viewer-error", null);
    optionalText("preview-note", null);
    text("build-id", "No file displayed");
    selectPart(null, "Select a file to see its components and print files.");
}

function updateLocation(project, file, replace = false) {
    const url = new URL(location.href);
    if (project) url.searchParams.set("project", project);
    else url.searchParams.delete("project");
    if (file) url.searchParams.set("file", file);
    else url.searchParams.delete("file");
    if (url.href !== location.href) history[replace ? "replaceState" : "pushState"]({}, "", url);
}

function defaultFile(project) {
    const selected = project?.files.find((file) => file.id === project.default_file);
    return selected || (project?.catalog_declared ? null : project?.files[0]) || null;
}

const expandedBuildGroups = new Set();
const selectedBuildTargets = new Map();

let workingTreeRequest = null;
let workingTreeProject = null;

async function refreshWorkingTree() {
    workingTreeRequest?.abort();
    const projectId = currentProject?.id;
    const badge = element("working-tree-status");
    badge.hidden = !projectId;
    if (!projectId) {
        workingTreeProject = null;
        return;
    }
    if (workingTreeProject !== projectId) {
        badge.textContent = "Checking working tree…";
        badge.dataset.state = "checking";
    }
    workingTreeProject = projectId;
    const request = new AbortController();
    workingTreeRequest = request;
    let state = "unavailable";
    let latestParts = null;
    try {
        const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/working-tree`,
            { signal: request.signal });
        if (!response.ok) throw new Error("Status unavailable");
        const result = await response.json();
        state = result.state;
        latestParts = result.latest_parts;
    } catch (error) {
        if (error.name === "AbortError") return;
    }
    if (workingTreeRequest !== request || currentProject?.id !== projectId) return;
    if (latestParts) {
        currentProject.latest_parts = latestParts;
        renderLatestParts();
    }
    badge.dataset.state = state;
    badge.textContent = state === "dirty" ? "Uncommitted changes"
        : state === "clean" ? "No uncommitted changes" : "Working tree status unavailable";
    badge.title = "Current project files, including staged and untracked changes. Refreshed every 5 seconds; independent of the displayed build.";
}

setInterval(() => {
    if (catalogMode && !document.hidden) void refreshWorkingTree();
}, 5000);
window.addEventListener("focus", () => {
    if (catalogMode) void refreshWorkingTree();
});

function renderLatestParts() {
    const list = element("latest-parts-list");
    const latest = currentProject?.latest_parts || [];
    text("latest-parts-heading", currentProject?.catalog_declared ? "Parts" : "Saved parts");
    optionalText("catalog-status", currentProject?.catalog_error || null);
    const signature = JSON.stringify([currentProject?.id, currentFile?.id, latest]);
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    list.replaceChildren();
    element("latest-parts-empty").hidden = latest.length > 0;
    for (const part of latest) {
        const file = currentProject.files.find(file => file.id === part.file_id);
        const project = currentProject;
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "file-button";
        const status = part.state === "current" ? "Up to date"
            : part.state === "changed" ? "Changes since build"
                : part.state === "missing" ? "No matching build" : "Build status unavailable";
        button.title = `${status}. Compared against project source and build inputs.${part.revision ? `\n${part.revision}` : ""}`;
        button.setAttribute("aria-label", `${part.label || part.name}: ${status}. ${file ? "View latest build" : "Select build configuration"}`);
        const title = document.createElement("span");
        title.className = "latest-part-title";
        const dot = document.createElement("span");
        dot.className = "build-dot";
        dot.dataset.state = part.state;
        dot.setAttribute("aria-hidden", "true");
        const name = document.createElement("span");
        name.className = "file-name";
        name.textContent = part.label || part.name.replaceAll("_", " ");
        title.append(dot, name);
        const detail = document.createElement("span");
        detail.className = "file-kind";
        const date = /^(\d{4})(\d{2})(\d{2})T/.exec(part.revision || "");
        detail.textContent = `${status}${date ? ` · ${date[1]}-${date[2]}-${date[3]}` : ""}`;
        if (part.review_state === "unrecorded") detail.textContent += " · Review not recorded";
        button.append(title, detail);
        button.addEventListener("click", async () => {
            if (part.target_id) selectedBuildTargets.set(project.id, part.target_id);
            buildOptionsKey = null;
            await chooseFile(project, file || null);
            if (file && currentProject === project && currentFile === file && activeAsset?.file === file.id) {
                selectPart(part.component);
            }
        });
        item.append(button);
        list.append(item);
    }
}

function renderFiles() {
    renderLatestParts();
    void syncBuildOptions();
    syncGitProject();
    void refreshWorkingTree();
    const list = element("files-list");
    const query = element("file-search").value.trim().toLocaleLowerCase();
    const allFiles = currentProject?.files || [];
    const files = allFiles;
    const matching = files.filter((file) => [file.name, file.id, file.group, file.kind, file.git?.message, file.git?.commit]
        .some((value) => typeof value === "string" && value.toLocaleLowerCase().includes(query)));
    list.replaceChildren();
    const noun = currentProject?.id === "cyclone" ? "build" : "file";
    text("file-count", currentProject
        ? query ? `${matching.length} of ${files.length} ${noun}s` : `${files.length} ${noun}${files.length === 1 ? "" : "s"}`
        : "");
    optionalText("project-status", !currentProject ? "Choose a project to browse its files."
        : !files.length ? "This project has no preview files yet."
            : !matching.length ? "No files match your search." : null);
    const groups = new Map();
    for (const file of matching) {
        const group = (currentProject.build_history || currentProject.id === "cyclone") ? file.git?.commit || "Unlinked builds" : file.group || "Files";
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(file);
    }
    const buildHistory = (currentProject?.build_history || currentProject?.id === "cyclone");
    const orderedGroups = [...groups].sort((a, b) => {
        if (!buildHistory) return 0;
        if (a[0] === "Unlinked builds") return 1;
        if (b[0] === "Unlinked builds") return -1;
        return (Date.parse(b[1][0].git.committed_at) || 0) - (Date.parse(a[1][0].git.committed_at) || 0);
    });
    for (const [name, filesInGroup] of orderedGroups) {
        const section = document.createElement(buildHistory ? "details" : "section");
        section.className = "file-group";
        const heading = document.createElement(buildHistory ? "summary" : "h3");
        const provenance = filesInGroup[0].git;
        heading.textContent = buildHistory && provenance
            ? `${provenance.message.split("\n")[0] || "Untitled commit"} · ${provenance.commit.slice(0, 7)} · ${new Date(provenance.committed_at).toLocaleDateString()}` : name;
        if (buildHistory) {
            heading.title = provenance?.message || "These builds have no recorded Git provenance.";
            section.open = Boolean(query) || expandedBuildGroups.has(name)
                || filesInGroup.some((file) => file.id === currentFile?.id);
            section.addEventListener("toggle", () => {
                if (section.open) expandedBuildGroups.add(name);
                else expandedBuildGroups.delete(name);
            });
        }
        const items = document.createElement("ul");
        for (const file of filesInGroup) {
            const item = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "file-button";
            button.dataset.fileId = file.id;
            button.setAttribute("aria-current", String(file.id === currentFile?.id));
            button.title = file.name;
            const label = document.createElement("span");
            label.className = "file-name";
            label.textContent = file.name;
            button.append(label);
            if (file.kind) {
                const kind = document.createElement("span");
                kind.className = "file-kind";
                kind.textContent = buildHistory ? file.git ? file.git.dirty ? "Uncommitted changes" : "Clean checkout" : "Git provenance unavailable" : file.kind;
                button.append(kind);
            }
            button.addEventListener("click", () => chooseFile(currentProject, file));
            item.append(button);
            items.append(item);
        }
        section.append(heading);
        if (buildHistory && provenance?.message.includes("\n")) {
            const description = document.createElement("p");
            description.className = "commit-message";
            description.textContent = provenance.message.split("\n").slice(1).join("\n").trim();
            section.append(description);
        }
        section.append(items);
        list.append(section);
    }
}

async function chooseFile(project, file, { replace = false, navigate = true } = {}) {
    if (graphicsLost) {
        showGraphicsFailure();
        return;
    }
    currentProject = project;
    currentFile = file;
    element("project-select").value = project?.id || "";
    element("file-search").disabled = !project;
    if (navigate) updateLocation(project?.id, file?.id, replace);
    renderFiles();
    clearPreview();
    element("file-heading").hidden = !project;
    text("current-project", [project?.name, file?.group].filter(Boolean).join(" / "));
    text("current-file", file?.name || "No file selected");
    document.title = file ? [file.name, file.group, project.name, "Workshop"].filter(Boolean).join(" · ")
        : "Workshop · CAD & print files";
    if (!file) {
        sceneState("empty", project?.catalog_declared ? "No preview selected" : "No files yet",
            project?.catalog_declared ? "Select a part and build its preview, or open a saved build from history."
                : "This project has no preview files available.");
        return;
    }
    sceneState("loading", "Loading preview…", [project.name, file.group, file.name].filter(Boolean).join(" / "));
    selectPart(null, "Loading the selected file and its print files…");
    sceneRequest = new AbortController();
    const expectedVersion = sceneVersion;
    try {
        const path = `/api/projects/${encodeURIComponent(project.id)}/files/${encodeURIComponent(file.id)}/scene`;
        const response = await fetch(`${path}?revision=${encodeURIComponent(file.revision)}`, { signal: sceneRequest.signal });
        if (!response.ok) {
            throw new Error(response.status === 409
                ? "This file has changed. Try again to load its latest revision."
                : `The preview is unavailable (HTTP ${response.status}).`);
        }
        const payload = await response.json();
        if (expectedVersion !== sceneVersion) return;
        const metadata = payload.metadata;
        if (metadata?.project_id !== project.id || metadata?.file_id !== file.id
            || metadata?.revision !== file.revision || !Array.isArray(metadata.parts)
            || metadata.parts.some((part) => !part || typeof part.name !== "string")) {
            throw new Error("The preview and print files do not match the selected file.");
        }
        if (payload.model?.type !== "data" || !payload.model.data?.shapes?.parts?.length) {
            throw new Error("This file does not contain a displayable model.");
        }
        activeAsset = { project: project.id, file: file.id, revision: metadata.revision, kind: file.kind };
        rendering = true;
        try {
            resetModel(payload.model, metadata);
            if (payload.model.config) payload.model.config.treeWidth = Math.min(payload.model.config.treeWidth || 240, 290);
            viewerPage.handleMessage(payload.model);
            if (!cadViewer?.ready) throw new Error("The model could not be displayed.");
            surface.dataset.modelReady = "true";
            surface.dataset.modelRevision = String(revision);
        } finally {
            rendering = false;
        }
        optionalText("preview-note", payload.model.preview?.simplified
            ? "Preview simplified for performance; downloads retain original detail." : null);
        sceneState(null);
        // The scene was constructed under the loading overlay. Repaint once
        // it is laid out and the loading overlay has been removed.
        requestAnimationFrame(() => {
            if (expectedVersion === sceneVersion) viewerPage.resize();
        });
    } catch (error) {
        if (error.name === "AbortError" || expectedVersion !== sceneVersion) return;
        clearPreview();
        sceneState("error", "Could not load this file", error.message, () => loadCatalog());
        selectPart(null, "Print files will appear when the selected preview loads.");
    }
}

function followLocation() {
    const query = new URLSearchParams(location.search);
    const projectId = query.get("project");
    const fileId = query.get("file");
    element("file-search").value = "";
    const project = projectId !== null ? projects.find((item) => item.id === projectId)
        : fileId === null ? projects.find((item) => item.id === "cyclone") || projects[0] : null;
    const file = fileId !== null ? project?.files.find((item) => item.id === fileId) : defaultFile(project);
    if ((projectId !== null && !project) || (fileId !== null && !file)) {
        currentProject = project || null;
        currentFile = null;
        element("project-select").value = project?.id || "";
        element("file-search").disabled = !project;
        element("file-heading").hidden = true;
        document.title = "File unavailable · Workshop";
        renderFiles();
        clearPreview();
        sceneState("error", project ? "File not found" : "Project not found",
            "The link does not match an available project and file. Choose one from the project browser.", () => loadCatalog());
        return;
    }
    if (!project) {
        currentProject = null;
        currentFile = null;
        element("file-heading").hidden = true;
        renderFiles();
        clearPreview();
        sceneState("empty", "No projects yet", "Projects will appear here when preview files are available.", () => loadCatalog());
        return;
    }
    void chooseFile(project, file || null, { replace: true });
}

async function loadCatalog() {
    const expectedVersion = ++catalogVersion;
    catalogRequest?.abort();
    catalogRequest = new AbortController();
    clearPreview();
    element("project-select").disabled = true;
    element("file-search").disabled = true;
    element("refresh-projects").disabled = true;
    element("files-list").replaceChildren();
    optionalText("project-status", "Loading projects…");
    sceneState("loading", "Loading projects…");
    try {
        const response = await fetch("/api/projects", { signal: catalogRequest.signal });
        if (!response.ok) throw new Error(`Projects are unavailable (HTTP ${response.status}).`);
        const catalog = await response.json();
        if (expectedVersion !== catalogVersion) return;
        if (!Array.isArray(catalog.projects)) throw new Error("The project list could not be read.");
        const projectIds = new Set();
        projects = catalog.projects.map((project) => {
            if (!project || typeof project.id !== "string" || !project.id || projectIds.has(project.id)
                || !Array.isArray(project.files)) throw new Error("The project list could not be read.");
            projectIds.add(project.id);
            const fileIds = new Set();
            return { ...project, name: project.name || project.id, files: project.files.map((file) => {
                if (!file || typeof file.id !== "string" || !file.id || fileIds.has(file.id)
                    || typeof file.revision !== "string" || !file.revision) throw new Error("The file list could not be read.");
                fileIds.add(file.id);
                const stamp = file.revision.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/);
                const saved = stamp ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
                    .format(new Date(`${stamp[1]}-${stamp[2]}-${stamp[3]}T${stamp[4]}:${stamp[5]}:${stamp[6]}Z`)) : file.revision;
                return { ...file,
                    name: (project.build_history || project.id === "cyclone") ? `${file.name || "Assembly"} · ${stamp ? saved : "Legacy export"}` : file.name || file.id,
                };
            }) };
        });
        const select = element("project-select");
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = projects.length ? "Select a project" : "No projects available";
        placeholder.disabled = true;
        select.replaceChildren(placeholder);
        for (const project of projects) {
            const option = document.createElement("option");
            option.value = project.id;
            option.textContent = project.name;
            select.append(option);
        }
        select.disabled = !projects.length;
        followLocation();
    } catch (error) {
        if (error.name === "AbortError" || expectedVersion !== catalogVersion) return;
        optionalText("project-status", "Projects could not be loaded.");
        sceneState("error", "Could not load projects", error.message, () => loadCatalog());
    } finally {
        if (expectedVersion === catalogVersion) element("refresh-projects").disabled = false;
    }
}

element("project-select").addEventListener("change", () => {
    const project = projects.find((item) => item.id === element("project-select").value);
    if (!project) return;
    element("file-search").value = "";
    void chooseFile(project, defaultFile(project));
});
element("file-search").addEventListener("input", renderFiles);
element("refresh-projects").addEventListener("click", () => loadCatalog());
element("retry-scene").addEventListener("click", () => retryScene?.());
window.addEventListener("popstate", () => {
    if (catalogMode) followLocation();
});

element("retry-downloads").addEventListener("click", () => {
    if (activeBuild) void loadPrintFiles(activeBuild, revision);
});

element("clear-selection").addEventListener("click", () => {
    if (cadViewer?.ready) {
        const id = cadViewer.lastBbox?.id;
        cadViewer.removeLastBbox();
        if (id) cadViewer.rendered.treeview.toggleLabelColor(null, id);
        cadViewer.clearSelection();
        cadViewer.update(true);
    }
    applySelection([]);
});

class EmbeddedViewer extends Viewer {
    constructor(...args) {
        if (safeGraphics) {
            const canvas = document.createElement("canvas");
            const gl = canvas.getContext("webgl2", {
                alpha: true, antialias: false, depth: true, stencil: true,
                powerPreference: "low-power", preserveDrawingBuffer: false,
            });
            if (!gl) throw new Error("WebGL 2 could not start. Quit and reopen your browser, or use Chrome.");
            args[1] = { ...args[1], canvas, gl };
        }
        super(...args);
        cadViewer = this;
        if (safeGraphics) this.renderer.setPixelRatio(1);
        this.renderer.domElement.addEventListener("webglcontextlost", (event) => {
            // Stop the renderer's automatic restore/recompile cycle. A lost
            // context requires an explicit reload, never an infinite retry.
            event.stopImmediatePropagation();
            graphicsLost = true;
            this.ready = false;
            this.continueAnimation = false;
            this.renderer.setAnimationLoop(null);
            sceneVersion += 1;
            sceneRequest?.abort();
            request?.abort();
            activeAsset = null;
            resetModel(null);
            showGraphicsFailure();
        }, true);
        // Resolve the click itself; lastObject may describe an older hover position.
        const canvas = this.renderer.domElement;
        let pointerDown = null;
        canvas.addEventListener("pointerdown", (event) => {
            pointerDown = event.button === 0 && event.isPrimary
                ? { x: event.clientX, y: event.clientY, revision,
                    camera: JSON.stringify(this.getCameraLocationSettings()) } : null;
        });
        canvas.addEventListener("pointercancel", () => { pointerDown = null; });
        canvas.addEventListener("pointerup", (event) => {
            const down = pointerDown;
            pointerDown = null;
            if (!down || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey
                || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 4
                || down.revision !== revision || rendering || surface.dataset.modelReady !== "true"
                || down.camera !== JSON.stringify(this.getCameraLocationSettings())) return;
            const rect = canvas.getBoundingClientRect();
            const x = event.clientX - rect.left, y = event.clientY - rect.top;
            if (x < 0 || y < 0 || x >= rect.width || y >= rect.height) return;
            const hit = this.idPicker?.pickAt(x, y);
            if (!hit || !this.pickingController.pickVisible(hit.info)) return;
            const highlight = this.rendered.nestedGroup.highlight;
            if (referenceHighlight && !referenceHighlight.wasSelected) {
                highlight.setSelected(referenceHighlight.id, false);
            }
            referenceHighlight = { id: hit.info.id, wasSelected: highlight.isSelected(hit.info.id) };
            highlight.setSelected(hit.info.id, true);
            captureReference({ backendId: hit.info.path, topo: hit.info.topo, asSolid: false, point: hit.point });
            this.update(true, false);
        });
        const pick = this.handlePick;
        this.handlePick = (path, name, meta, shift, ...rest) => {
            pick(path, name, meta, shift, ...rest);
            // lastPick is deduplicated upstream: clearing and reselecting the
            // same component can emit nothing, as can toggling a pick off.
            // Read the resulting selection for every ordinary pick instead.
            if (!meta && !shift && !rendering) {
                applySelection(this.lastBbox ? [this.lastBbox.id] : []);
            }
        };
        const clear = this.pickingController.clearSelection;
        this.pickingController.clearSelection = (...args) => {
            clear(...args);
            // Escape and leaving the Select tool clear the objects after the
            // tool's final notification. Synchronize only once that is done.
            if (!rendering) applySelection([]);
        };
        const selectTool = this.cadTools.selectObject;
        const notifySelection = selectTool.notify.bind(selectTool);
        selectTool.notify = () => {
            notifySelection();
            // Different parts (or models) can select the same face number,
            // causing the upstream numeric status to be deduplicated. This
            // hook runs for each actual tool selection, toggle, and removal.
            if (!rendering) applySelection(selectTool.selectedShapes.map((shape) => shape.backendId));
        };
        // Also useful when inspecting a model from the browser's console.
        window.cadViewer = this;
    }
}

function showGraphicsFailure() {
    sceneState("error", "3D graphics stopped",
        "The browser lost its graphics context. Quit and reopen Safari if reloading does not help; Chrome is another option.",
        () => location.reload());
    text("retry-scene", "Reload preview");
    selectPart(null, "Reload the preview to restore the model and its print files.");
}

async function start() {
    const response = await fetch("/api/viewer-config");
    if (!response.ok) throw new Error("Could not load the viewer settings. Refresh to retry.");
    const settings = await response.json();
    catalogMode = settings.catalog_enabled === true && new URLSearchParams(location.search).get("live") !== "1";
    element("project-panel").hidden = !catalogMode;
    document.querySelector(".workspace").classList.toggle("catalog-mode", catalogMode);
    let comms;
    const page = createPage({
        Viewer: EmbeddedViewer, Display, Timer,
        container: surface,
        getSize: () => ({ width: surface.clientWidth, height: surface.clientHeight }),
        listen: false,
        theme: settings.theme,
        send(command, message) {
            if (command === "status") onViewerStatus(message);
            comms?.sendStatus({ command, text: message });
        },
        overrides: {
            display: {
                treeWidth: Math.min(settings.tree_width || 240, 290),
                glass: settings.glass, tools: settings.tools, theme: settings.theme, zscaleTool: false,
            },
            viewer: { tools: settings.tools, glass: settings.glass, up: settings.up, control: settings.control },
        },
    });
    viewerPage = page;
    const resizeObserver = new ResizeObserver(() => page.resize());
    resizeObserver.observe(surface);
    if (catalogMode) {
        await loadCatalog();
        return;
    }
    page.showSplash({ theme: settings.theme, treeWidth: 240, keymap: settings.modifier_keys });
    window.addEventListener("message", (event) => {
        // Comms relays the local WebSocket through a same-window message.
        if (event.source !== window || event.origin !== location.origin) return;
        let message;
        try {
            message = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
        } catch { return; }
        if (!message || typeof message !== "object") return;
        const newModel = message.type === "data" || message.type === "logo" || message.type === "clear";
        if (newModel) resetModel(message.type === "data" ? message : null);
        if (message.type === "data" && message.config) {
            message.config.treeWidth = Math.min(message.config.treeWidth || 240, 290);
        }
        rendering = newModel;
        try {
            page.handleMessage(message);
            if (message.type === "data") {
                surface.dataset.modelReady = String(Boolean(cadViewer?.ready));
                surface.dataset.modelRevision = String(revision);
            }
            optionalText("viewer-error", null);
        } catch (error) {
            if (newModel) resetModel(null);
            optionalText("viewer-error", `The model could not be displayed: ${error.message}`);
            throw error;
        } finally {
            rendering = false;
        }
    });
    comms = new Comms(location.hostname, location.port, settings.max_reconnect_attempts ?? undefined);
}

start().catch((error) => {
    optionalText("viewer-error", error.message);
    text("print-status", "The viewer could not start. Refresh to retry.");
    console.error(error);
});

let gitProject = null;
let gitState = null;
let gitBusy = false;
let gitVersion = 0;

function gitCommand(action, payload = {}) {
    const project = gitProject;
    return new Promise((resolve, reject) => {
        const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/git`);
        const timer = setTimeout(() => { socket.close(); reject(new Error("Request timed out. Refresh changes before retrying.")); }, 150000);
        socket.onopen = () => socket.send(JSON.stringify({ project, action, ...payload }));
        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.error) reject(new Error(data.error));
                else resolve(data.result);
            } catch (error) { reject(error); }
            socket.close();
        };
        socket.onerror = () => reject(new Error("Could not connect to Git service."));
        socket.onclose = () => { clearTimeout(timer); reject(new Error("Git connection closed. Refresh changes to check the result.")); };
    });
}
function gitButtons() {
    element("git-refresh").disabled = gitBusy || !gitProject;
    element("git-stage").disabled = gitBusy || !element("git-files").querySelector("input:checked");
    element("git-draft").disabled = gitBusy || !gitState?.has_staged;
    element("git-commit").disabled = gitBusy || !gitState?.has_staged || !element("git-message").value.trim();
}
function renderGit(state) {
    gitState = state;
    const list = element("git-files");
    list.replaceChildren();
    for (const file of state.files) {
        const label = document.createElement("label");
        const check = document.createElement("input");
        check.type = "checkbox";
        check.value = file.path;
        check.disabled = file.status[1] === " ";
        check.addEventListener("change", gitButtons);
        label.append(check, document.createTextNode(`${file.status} ${file.path}${file.staged ? " (staged)" : ""}`));
        list.append(label);
    }
    text("git-summary", state.files.length ? `${state.files.filter(f => f.staged).length} staged files. All staged changes will be committed.` : "Working tree is clean.");
    gitButtons();
}
function syncGitProject() {
    if (gitProject === (currentProject?.id || null)) return;
    gitProject = currentProject?.id || null;
    gitVersion++;
    gitBusy = false;
    gitState = null;
    element("git-files").replaceChildren();
    element("git-message").value = "";
    text("git-summary", "");
    text("git-status", "");
    gitButtons();
    if (element("git-panel").open && gitProject) void runGit("status");
}
async function runGit(action) {
    if (gitBusy || !gitProject) return;
    gitBusy = true;
    const version = gitVersion;
    const originalMessage = element("git-message").value;
    gitButtons();
    text("git-status", action === "message" ? "Drafting commit message…" : "Working…");
    try {
        const result = await gitCommand(action, {
            paths: [...element("git-files").querySelectorAll("input:checked")].map(input => input.value),
            snapshot: gitState?.snapshot, message: originalMessage,
        });
        if (version !== gitVersion) return;
        if (action === "message") {
            if (element("git-message").value === originalMessage) element("git-message").value = result.message;
            text("git-status", "AI draft ready. Review the message before committing.");
        } else {
            renderGit(result);
            text("git-status", result.commit ? `Committed ${result.commit}.` : "Changes refreshed.");
            if (result.commit) element("git-message").value = "";
            if (action === "stage" && !element("git-message").value.trim()) {
                gitBusy = false;
                await runGit("message");
            }
            void refreshWorkingTree();
        }
    } catch (error) {
        if (version === gitVersion) text("git-status", error.message);
    } finally {
        if (version === gitVersion) { gitBusy = false; gitButtons(); }
    }
}
element("git-panel").addEventListener("toggle", () => {
    if (element("git-panel").open) void runGit("status");
});
element("git-refresh").addEventListener("click", () => runGit("status"));
element("git-stage").addEventListener("click", () => runGit("stage"));
element("git-draft").addEventListener("click", () => runGit("message"));
element("git-commit").addEventListener("click", () => runGit("commit"));
element("git-message").addEventListener("input", gitButtons);

function updateAgentButton() {
    element("send-agent").disabled = agentBusy || buildBusy || !geometryReference?.project
        || !geometryReference?.file || !element("agent-message").value.trim();
}
element("agent-message").addEventListener("input", updateAgentButton);
element("send-agent").addEventListener("click", async () => {
    if (agentBusy || !geometryReference?.project) return;
    const message = element("agent-message").value.trim();
    if (!message) return;
    const reference = structuredClone(geometryReference);
    agentBusy = true;
    updateBuildButton();
    updateAgentButton();
    element("agent-reply").hidden = true;
    text("agent-status", `Codex is working on ${reference.project}: ${reference.geometry_path}…`);
    try {
        const result = await new Promise((resolve, reject) => {
            const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/agent`);
            socket.onopen = () => socket.send(JSON.stringify({ project: reference.project, message, reference }));
            socket.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.error) reject(new Error(data.error));
                    else resolve(data.result);
                } catch (error) { reject(error); }
                socket.close();
            };
            socket.onerror = () => reject(new Error("Could not connect to the agent service."));
            socket.onclose = () => reject(new Error("Agent connection closed. The task may still be running; review changes before resending."));
        });
        text("agent-status", `Codex replied for ${reference.project}: ${reference.geometry_path}. Read the reply to check whether the requested change was completed.`);
        text("agent-reply", result.reply);
        element("agent-reply").hidden = false;
        void refreshWorkingTree();
        if (gitProject === reference.project && !gitBusy) void runGit("status");
    } catch (error) {
        text("agent-status", `${reference.project}: ${error.message}`);
    } finally {
        agentBusy = false;
        updateBuildButton();
        updateAgentButton();
    }
});

function buildRequest(action, payload, progress = () => {}) {
    return new Promise((resolve, reject) => {
        const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/build`);
        socket.onopen = () => socket.send(JSON.stringify({ action, ...payload }));
        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.progress !== undefined) { progress(data.progress); return; }
                if (data.error) reject(new Error(data.error));
                else resolve(data.result);
            } catch (error) { reject(error); }
            socket.close();
        };
        socket.onerror = () => reject(new Error("Could not connect to the build service."));
        socket.onclose = () => reject(new Error("Build connection closed. Refresh the file list to check for a completed build before retrying."));
    });
}
function updateBuildButton() {
    element("build-preview").disabled = buildBusy || agentBusy || !element("build-configuration").value;
    element("build-configuration").disabled = buildBusy || !element("build-configuration").options.length;
}
async function syncBuildOptions(preserveStatus = false) {
    const payload = { project: currentProject?.id, file: currentFile?.id, revision: currentFile?.revision };
    const key = JSON.stringify([payload, currentProject?.catalog_revision]);
    if (buildBusy || key === buildOptionsKey) return;
    buildOptionsKey = key;
    const version = ++buildOptionsVersion;
    const supported = currentProject?.catalog_declared && ["vault", "cyclone"].includes(payload.project);
    element("preview-build").hidden = !supported;
    element("build-configuration").replaceChildren();
    updateBuildButton();
    if (!supported) return;
    if (!preserveStatus) text("build-preview-status", "Loading build configuration…");
    try {
        const result = await buildRequest("options", payload);
        if (version !== buildOptionsVersion) return;
        const select = element("build-configuration");
        const placeholder = new Option("Choose configuration", "");
        placeholder.disabled = true;
        select.append(placeholder);
        for (const option of result.options) select.append(new Option(option.label, option.id));
        const preferred = selectedBuildTargets.get(payload.project);
        select.value = result.options.some(option => option.id === preferred) ? preferred : result.default || "";
        selectedBuildTargets.set(payload.project, select.value);
        if (!preserveStatus) text("build-preview-status", "");
    } catch (error) {
        if (version === buildOptionsVersion) {
            buildOptionsKey = null;
            text("build-preview-status", error.message);
        }
    } finally { if (version === buildOptionsVersion) updateBuildButton(); }
}
element("build-configuration").addEventListener("change", () => {
    selectedBuildTargets.set(currentProject.id, element("build-configuration").value);
    updateBuildButton();
});
element("build-preview").addEventListener("click", async () => {
    if (buildBusy || agentBusy || !element("build-configuration").value) return;
    const payload = { project: currentProject.id, file: currentFile?.id, revision: currentFile?.revision,
        configuration: element("build-configuration").value };
    const originalSceneVersion = sceneVersion;
    buildBusy = true;
    updateBuildButton();
    updateAgentButton();
    element("build-progress").hidden = false;
    element("build-output").hidden = false;
    text("build-output", "");
    text("build-preview-status", `Building ${payload.project}…`);
    try {
        const result = await buildRequest("build", payload, chunk => {
            const output = element("build-output");
            output.textContent = (output.textContent + chunk).slice(-50000);
            output.scrollTop = output.scrollHeight;
        });
        if (sceneVersion === originalSceneVersion && currentProject?.id === payload.project) {
            updateLocation(result.project, result.file);
            await loadCatalog();
            text("build-preview-status", "Build succeeded. Loading the new preview…");
        } else {
            text("build-preview-status", `Build succeeded for ${payload.project}. Refresh its file list to view it.`);
        }
    } catch (error) { text("build-preview-status", error.message); }
    finally {
        buildBusy = false;
        element("build-progress").hidden = true;
        updateBuildButton();
        updateAgentButton();
        // Refresh configuration on the next selection, without erasing this build's result.
        buildOptionsKey = null;
        if (currentProject?.id !== payload.project || currentFile?.id !== payload.file) void syncBuildOptions(true);
    }
});
