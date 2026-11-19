#!/usr/bin/env python3
"""Accessibility touch-ups applied to the rendered HTML."""
import re, glob, os
out = os.path.join(os.environ.get("QUARTO_PROJECT_OUTPUT_DIR", "_book"), "*.html")
for f in glob.glob(out):
    h = open(f, encoding="utf-8").read(); o = h
    # WCAG 2.5.3: the accessible name of the pagination links must contain their visible text,
    # so drop the aria-label (the visible text already is the name)
    h = re.sub(r'(<a [^>]*class="pagination-link")\s+aria-label="[^"]*"', r"\1", h)
    h = re.sub(r'(<a [^>]*)aria-label="[^"]*"([^>]*class="pagination-link")', r"\1\2", h)
    # WCAG 2.1.1: a table box that scrolls sideways must be reachable by keyboard.
    # Only boxes that actually overflow get a tab stop (checked on load and on resize).
    if "popgen-scroll-focus" not in h:
        h = h.replace("</body>", """<script id="popgen-scroll-focus">
(function(){function f(){document.querySelectorAll('figure.quarto-float-tbl > div, .column-page-right > div').forEach(function(d){
 var s=d.scrollWidth>d.clientWidth+1; if(s){d.setAttribute('tabindex','0');d.setAttribute('role','region');d.setAttribute('aria-label','Scrollable table');}
 else{d.removeAttribute('tabindex');}});}
 window.addEventListener('load',f);window.addEventListener('resize',f);})();
</script></body>""")

    # No overlaps: after Quarto positions margin notes/figures, push any margin item down
    # until it clears wide figures/tables/display equations and earlier margin items.
    if "popgen-no-overlap" not in h:
        h = h.replace("</body>", """<script id="popgen-no-overlap">
(function(){
 var SEL='.column-margin.column-container > *, .margin-caption, .aside';
 function R(e){var r=e.getBoundingClientRect();return {t:r.top+scrollY,b:r.bottom+scrollY,l:r.left,r:r.right};}
 function run(){
  var root=document.getElementById('quarto-document-content'); if(!root) return;
  var items=[].filter.call(root.querySelectorAll(SEL),function(e){return e.offsetParent!==null && !e.parentElement.closest(SEL);});
  items.forEach(function(e){e.style.marginTop='';});
  var blk=[].filter.call(root.querySelectorAll('figure, table, pre, .column-page-right, .column-page, .column-screen, mjx-container[display="true"], .callout, .chapter-summary, blockquote'),
    function(e){return e.offsetParent!==null && !e.closest(SEL) && !(e.parentElement.closest('figure,.column-page-right,.column-page,.callout') && e.tagName!=='FIGURE' && false);});
  var last=0;
  for(var pass=0;pass<3;pass++){
   last=0; var moved=false;
   items.forEach(function(it){
    var r=R(it), top=r.t, h=r.b-r.t, need=Math.max(top,last), gap=8, again=true, n=0;
    while(again && n++<40){
     again=false;
     blk.forEach(function(b){
      if(it.contains(b)||b.contains(it)) return;
      var q=R(b); if(q.r<=r.l+1||q.l>=r.r-1) return;
      if(need<q.b+gap && need+h>q.t-1 && q.b>q.t){ if(need<q.b+gap){need=q.b+gap;again=true;} }
     });
    }
    if(need>top+1){var cur=parseFloat(getComputedStyle(it).marginTop)||0; it.style.marginTop=(cur+need-top)+'px'; moved=true;}
    var r2=R(it); last=r2.b+ (parseFloat(getComputedStyle(it).marginBottom)||0);
   });
   if(!moved) break;
  }
 }
 var tm; function later(){clearTimeout(tm);tm=setTimeout(run,120);}
 window.addEventListener('load',function(){later();
   if(document.fonts&&document.fonts.ready) document.fonts.ready.then(later);
   if(window.MathJax&&MathJax.startup&&MathJax.startup.promise) MathJax.startup.promise.then(later);
   [].forEach.call(document.images,function(i){if(!i.complete) i.addEventListener('load',later);});
   setTimeout(later,1500);});
 window.addEventListener('resize',later);
 if(window.ResizeObserver) new ResizeObserver(later).observe(document.body);
})();
</script></body>""")
    if h != o:
        open(f, "w", encoding="utf-8").write(h)
