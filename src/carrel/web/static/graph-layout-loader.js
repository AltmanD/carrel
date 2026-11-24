/* Layout runs inside a local, terminable Worker. No ELK on the UI thread. */
(function () {
  'use strict';
  const source = new URL(document.currentScript.src);
  const workerURL = new URL('graph-layout-worker.js' + source.search, source);
  const cache = new Map();
  window.CarrelGraphLayout = {
    create() {
      let worker, pending, generation = 0;
      const cancel = () => {
        generation++;
        worker?.terminate(); worker = null;
        pending?.reject(new DOMException('Layout superseded', 'AbortError')); pending = null;
      };
      return {
        terminateWorker: cancel,
        layout(graph) {
          cancel();
          const key = JSON.stringify(graph);
          if (cache.has(key)) return Promise.resolve(structuredClone(cache.get(key)));
          const current = generation;
          return new Promise((resolve, reject) => {
            pending = { reject };
            // Coalesce same-frame requests before allocating a Worker.
            requestAnimationFrame(() => {
              if (current !== generation) return;
              worker = new Worker(workerURL);
              worker.onmessage = ({ data }) => {
                pending = null;
                worker?.terminate(); worker = null;
                if (data.error) reject(new Error(data.error)); else {
                  if (cache.size >= 24) cache.delete(cache.keys().next().value);
                  cache.set(key, structuredClone(data.result));
                  resolve(data.result);
                }
              };
              worker.onerror = event => { pending = null; reject(new Error(event.message || 'Layout worker failed')); };
              worker.postMessage(graph);
            });
          });
        },
      };
    },
  };
})();
