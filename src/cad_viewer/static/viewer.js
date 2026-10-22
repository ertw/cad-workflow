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
    return project?.files.find((file) => file.id === project.default_file) || project?.files[0] || null;
}

function renderFiles() {
    const list = element("files-list");
    const query = element("file-search").value.trim().toLocaleLowerCase();
    const allFiles = currentProject?.files || [];
    element("previous-designs-control").hidden = !allFiles.some((file) => file.historical);
    const files = allFiles.filter((file) => !file.historical || element("previous-designs").checked);
    const matching = files.filter((file) => [file.name, file.id, file.group, file.kind]
        .some((value) => typeof value === "string" && value.toLocaleLowerCase().includes(query)));
    list.replaceChildren();
    text("file-count", currentProject
        ? query ? `${matching.length} of ${files.length} files` : `${files.length} ${files.length === 1 ? "file" : "files"}`
        : "");
    optionalText("project-status", !currentProject ? "Choose a project to browse its files."
        : !files.length ? "This project has no preview files yet."
            : !matching.length ? "No files match your search." : null);
    const groups = new Map();
    for (const file of matching) {
        const group = file.group || "Files";
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(file);
    }
    for (const [name, filesInGroup] of groups) {
        const section = document.createElement("section");
        section.className = "file-group";
        const heading = document.createElement("h3");
        heading.textContent = name;
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
                kind.textContent = file.kind;
                button.append(kind);
            }
            button.addEventListener("click", () => chooseFile(currentProject, file));
            item.append(button);
            items.append(item);
        }
        section.append(heading, items);
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
        sceneState("empty", "No files yet", "This project has no preview files available.");
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
    element("previous-designs").checked = Boolean(file?.historical);
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
                const historical = project.id === "cyclone" && /^Configuration [a-f0-9]{8}$/.test(file.name);
                const stamp = file.revision.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/);
                const saved = stamp ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
                    .format(new Date(`${stamp[1]}-${stamp[2]}-${stamp[3]}T${stamp[4]}:${stamp[5]}:${stamp[6]}Z`)) : file.revision;
                return { ...file, historical,
                    name: historical ? `Saved assembly · ${saved}`
                        : project.id === "cyclone" && file.name === "Baseline" ? "Baseline · full assembly" : file.name || file.id,
                    group: historical ? "Previous designs" : file.group,
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
element("previous-designs").addEventListener("change", renderFiles);
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
