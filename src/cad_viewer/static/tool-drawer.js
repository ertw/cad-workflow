// The launcher owns search and navigation; tools own their controls and state.
export function createToolDrawer({ tools, getProjects, chooseProject, onTool = () => {} }) {
    const drawer = document.getElementById("tool-drawer");
    const input = document.getElementById("tool-search");
    const results = document.getElementById("tool-results");
    const toggle = document.getElementById("tool-toggle");
    const workspace = document.querySelector(".workspace");
    let active = tools[0], matches = [], index = 0, searching = false, filter = null;

    function commands() {
        return [...tools.map(tool => ({ ...tool, kind: "Tool", key: `tool:${tool.id}` })),
            ...getProjects().map(project => ({ id: project.id, label: project.name,
                aliases: project.id, kind: "Project", key: `project:${project.id}` }))];
    }

    function expand(expanded) {
        workspace.classList.toggle("tools-collapsed", !expanded);
        toggle.setAttribute("aria-expanded", String(expanded));
        toggle.setAttribute("aria-label", expanded ? "Collapse tools" : "Expand tools");
        toggle.textContent = expanded ? "›" : "‹";
    }

    function render(query = null) {
        filter = query;
        matches = commands().map(command => ({ ...command, score: query === null ? 0
            : Math.max(fuzzyScore(query, command.label), fuzzyScore(query, command.aliases || "")) }))
            .filter(command => command.score >= 0)
            .sort((a, b) => b.score - a.score);
        index = query === null ? Math.max(0, matches.findIndex(item => item.key === `tool:${active.id}`)) : 0;
        results.replaceChildren();
        for (const [position, command] of matches.entries()) {
            const option = document.createElement("button");
            option.type = "button";
            option.id = `command-${position}`;
            option.role = "option";
            option.tabIndex = -1;
            option.dataset.commandId = command.key;
            const label = document.createElement("span"), kind = document.createElement("small");
            label.textContent = command.label;
            kind.textContent = command.kind;
            option.append(label, kind);
            option.addEventListener("pointerdown", event => event.preventDefault());
            option.addEventListener("click", () => activate(position));
            results.append(option);
        }
        if (!matches.length) {
            const empty = document.createElement("p");
            empty.textContent = "No matches";
            empty.role = "status";
            results.append(empty);
        }
        updateSelection();
    }

    function updateSelection() {
        [...results.querySelectorAll('[role="option"]')].forEach((option, position) => {
            option.setAttribute("aria-selected", String(position === index));
        });
        if (matches[index]) input.setAttribute("aria-activedescendant", `command-${index}`);
        else input.removeAttribute("aria-activedescendant");
        results.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    }

    function close() {
        searching = false;
        results.hidden = true;
        input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
        input.value = active.label;
    }

    function open() {
        expand(true);
        searching = true;
        results.hidden = false;
        input.setAttribute("aria-expanded", "true");
        input.value = active.label;
        render();
        input.focus({ preventScroll: true });
        input.select();
        if (matchMedia("(max-width: 780px)").matches) drawer.scrollIntoView({ block: "start" });
    }

    function selectTool(id) {
        const tool = tools.find(item => item.id === id);
        if (!tool) return;
        active = tool;
        drawer.dataset.tool = id;
        for (const item of tools) document.getElementById(`tool-${item.id}`).hidden = item.id !== id;
        close();
        onTool(id);
    }

    function activate(position = index) {
        const command = matches[position];
        if (!command) return;
        if (command.kind === "Tool") selectTool(command.id);
        else { close(); chooseProject(command.id); }
        input.select();
    }

    input.addEventListener("click", () => { if (!searching) open(); });
    input.addEventListener("input", () => {
        searching = true;
        results.hidden = false;
        input.setAttribute("aria-expanded", "true");
        render(input.value);
    });
    input.addEventListener("keydown", event => {
        if (event.isComposing) return;
        if (["ArrowDown", "ArrowUp"].includes(event.key)) {
            event.preventDefault();
            if (!searching) open();
            index = (index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % (matches.length || 1);
            updateSelection();
        } else if (event.key === "Enter") {
            event.preventDefault();
            if (searching) activate();
            else selectTool(active.id);
        } else if (event.key === "Escape") {
            event.preventDefault();
            close();
            input.select();
        } else if (event.key === "Tab") close();
    });
    document.addEventListener("pointerdown", event => {
        if (!event.target.closest(".tool-launcher")) close();
    });
    window.addEventListener("keydown", event => {
        if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
            event.preventDefault();
            if (!event.repeat) open();
        }
    });
    toggle.addEventListener("click", () => {
        const expanded = workspace.classList.contains("tools-collapsed");
        close();
        expand(expanded);
        if (expanded) open();
    });
    selectTool(active.id);
    input.focus({ preventScroll: true });
    input.select();
    return { open, selectTool, refresh: () => { if (searching) render(filter); } };
}

function fuzzyScore(query, label) {
    query = query.toLocaleLowerCase().replace(/\s/g, "");
    label = label.toLocaleLowerCase();
    if (!query) return 0;
    if (label === query) return 1000;
    let previous = -1, score = 0;
    for (const letter of query) {
        const position = label.indexOf(letter, previous + 1);
        if (position < 0) return -1;
        score += 10 + (position === previous + 1 ? 8 : 0) + (position === 0 || label[position - 1] === " " ? 6 : 0);
        score -= position - previous - 1;
        previous = position;
    }
    return Math.max(0, score);
}
