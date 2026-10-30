// Each command has one response, with optional progress messages beforehand.
export function commandRequest(service, payload, { progress = () => {}, timeout = 0,
    closed = "Connection closed. Refresh to check the result before retrying." } = {}) {
    return new Promise((resolve, reject) => {
        const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/${service}`);
        let settled = false, timer;
        const finish = (error, result) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            if (error) reject(error); else resolve(result);
            socket.close();
        };
        if (timeout) timer = setTimeout(() => finish(new Error(closed)), timeout);
        socket.onopen = () => socket.send(JSON.stringify(payload));
        socket.onmessage = event => {
            try {
                const data = JSON.parse(event.data);
                if (data.progress !== undefined) { progress(data.progress); return; }
                finish(data.error ? new Error(data.error) : null, data.result);
            } catch (error) { finish(error); }
        };
        socket.onerror = () => finish(new Error(`Could not connect to the ${service} service.`));
        socket.onclose = () => finish(new Error(closed));
    });
}
