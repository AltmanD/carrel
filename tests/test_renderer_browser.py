"""Isolated renderer contract checks; no application data or server writes."""
from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest

@pytest.mark.browser
def test_renderer_browser():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    static = Path(__file__).parents[1] / "src/carrel/web/static"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(static)))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=[f'--explicitly-allowed-ports={server.server_port}'])
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.set_content("<div id=graph></div><style>.graph-stage {height:600px;width:900px}</style>")
            page.add_style_tag(url=f"http://127.0.0.1:{server.server_port}/graph.css")
            for script in ("graph-layout-loader.js", "graph.js"):
                page.add_script_tag(url=f"http://127.0.0.1:{server.server_port}/{script}")
            result = page.evaluate("""async () => {
              const root=document.getElementById("graph");
              const node=(id,label,focus=false)=>({id,label,title:label,role:"paper",focus,meta:{paper_id:id,branch:{key:"domain",label:"测试领域"}}});
              const nodes=[node("a","Single"),node("b","A very long multiline method name with complete words preserved"),node("c","中文与 English 方法混合"),node("d","Focus",true)];
              let c=CarrelGraph.mount(root,{scope:"library",nodes,edges:[]},{managed:true}); await c.ready;
              const previousController=c;
              c.destroy();
              c=CarrelGraph.mount(root,{scope:"library",nodes,edges:[]},{managed:true}); await c.ready;
              const oldTransform={...previousController.transform};
              c.canvas.dispatchEvent(new WheelEvent('wheel',{deltaY:-100,clientX:200,clientY:200,cancelable:true}));
              if(JSON.stringify(oldTransform)!==JSON.stringify(previousController.transform)) throw Error("destroyed event handler ran");
              if(c.regions[0].title!=="测试领域") throw Error("branch grouping");
              c.cullViewport(true);
              const geometry=()=>[...c.nodes.values()].map(n=>{const boxes=[...n.el.querySelectorAll("text")].map(t=>t.getBoundingClientRect()); const r=n.shape.getBoundingClientRect(); return Math.abs((Math.min(...boxes.map(b=>b.top))+Math.max(...boxes.map(b=>b.bottom)))/2-(r.top+r.bottom)/2)});
              const offsets=geometry();
              for (const n of c.nodes.values()) {
                const r=n.shape.getBoundingClientRect();
                for(const text of n.el.querySelectorAll("text")) {const b=text.getBoundingClientRect(); offsets.push(Math.abs((b.left+b.right-r.left-r.right)/2));}
              }
              const before=[...c.nodes.values()].map(n=>[n.id,n.x,n.y]);
              const element=c.nodes.get("a").el; const view={...c.transform};
              c=CarrelGraph.mount(root,{scope:"library",focus:"a",nodes:[nodes[0],node("new","Neighbor")],edges:[{source:"a",target:"new",role:"cites"}]},{managed:true}); await c.ready;
              if(before.some(([id,x,y])=>c.nodes.get(id).x!==x||c.nodes.get(id).y!==y)) throw Error("positions moved");
              if(JSON.stringify(view)!==JSON.stringify(c.transform)) throw Error("view moved");
              if(c.nodes.get("a").el!==element) throw Error("node identity lost");
              c.transform.x=-10000; c.applyTransform();
              if(c.nodeLayer.children.length) throw Error("culling failed");
              const expanded=c.buildExportSvg(false), viewport=c.buildExportSvg(true);
              await new Promise((resolve,reject)=>{const image=new Image(); image.onload=()=>{const canvas=document.createElement("canvas"); canvas.width=image.width; canvas.height=image.height; canvas.getContext("2d").drawImage(image,0,0); canvas.toBlob(blob=>blob?.size ? resolve() : reject(Error("PNG empty")));}; image.onerror=reject; image.src=URL.createObjectURL(new Blob([expanded],{type:"image/svg+xml"}));});
              if(!expanded.includes("Neighbor")||!expanded.includes("当前已展开内容")||!viewport.includes("当前视口")) throw Error("export scope");
              c=CarrelGraph.mount(root,{scope:"motivation_tree",nodes,edges:[{source:"a",target:"b",role:"motivation",primary:true}]},{managed:true}); await c.ready;
              if([...c.nodes.values()].some(n=>n.layoutHidden||!Number.isFinite(n.x))) throw Error("disconnected nodes hidden");
              c.cullViewport(true); offsets.push(...geometry());
              if(!c.edgeLayer) throw Error(c.status.textContent); if(!c.edgeLayer.querySelector("[marker-end]")) throw Error("missing arrow");
              return {offsets, routes:c.routes.size};
            }""")
            assert max(result["offsets"]) <= 2, result
            assert result["routes"] == 1
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
