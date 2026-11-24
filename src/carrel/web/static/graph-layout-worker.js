/* Instantiate the bundled module to register its native Worker dispatcher. */
importScripts('vendor/elk.bundled.js' + self.location.search);
try { new ELK(); } catch (error) { if (typeof self.onmessage !== 'function') throw error; }
const dispatch = self.onmessage;
const send = self.postMessage.bind(self);
self.postMessage = message => {
  if (message.id === 'layout') send(message.error ? {error: String(message.error)} : {result: message.data});
};
dispatch({data: {id: 'register', cmd: 'register', algorithms: ['layered']}});
self.onmessage = ({data}) => dispatch({data: {id: 'layout', cmd: 'layout', graph: data}});
