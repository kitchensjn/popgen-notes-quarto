-- popgen.lua : pandoc Lua filter used by tools/convert.py
local MARK = "ZZMARGINZZ"

local function flatten(blocks)
  local out = pandoc.List()
  for _, b in ipairs(blocks) do
    if #out > 0 then out:insert(pandoc.Space()) end
    if b.t == "Para" or b.t == "Plain" then
      out:extend(b.content)
    elseif b.t == "BlockQuote" or b.t == "Div" then
      out:extend(flatten(b.content))
    elseif b.t == "BulletList" or b.t == "OrderedList" then
      for _, item in ipairs(b.content) do out:extend(flatten(item)); out:insert(pandoc.Space()) end
    else
      out:insert(pandoc.Str(pandoc.utils.stringify(b)))
    end
  end
  return out
end

function Note(el)
  local first = el.content[1]
  if first and (first.t == "Para" or first.t == "Plain")
     and first.content[1] and first.content[1].t == "Str"
     and first.content[1].text == MARK then
    first.content:remove(1)
    if first.content[1] and first.content[1].t == "Space" then first.content:remove(1) end
    return pandoc.Span(flatten(el.content), pandoc.Attr("", { "aside" }))
  end
end

function Span(el)
  if el.attributes["label"] then el.attributes["label"] = nil end
  return el
end

function Header(el)
  if not el.identifier:match("^sec%-") then el.identifier = "" end
  return el
end

-- Starting-point alt text: the first sentence of the caption (to be refined by the author).
-- Math is turned into plain readable text, image credits are left out.
local SYM = { leq="<=", le="<=", geq=">=", ge=">=", times="x", approx="approximately", ll="<<", gg=">>",
              sim="~", cdot="*", infty="infinity", prime="'", rightarrow="->", mu="mu", pi="pi", tau="tau",
              lambda="lambda", rho="rho", sigma="sigma", beta="beta", alpha="alpha", theta="theta",
              Delta="Delta", gamma="gamma", nu="nu", ell="l", partial="d" }
local function plain_math(tex)
  tex = tex:gsub("\\(%a+)", function(c) return SYM[c] or "" end)
  tex = tex:gsub("[{}\\]", ""):gsub("%s+", " ")
  return tex
end

local function alt_from_caption(inlines)
  local clean = pandoc.walk_inline(pandoc.Span(inlines), {
    Math = function(m) return pandoc.Str(plain_math(m.text)) end,
  })
  local txt = pandoc.utils.stringify(clean):gsub("%s+", " ")
  local cut = txt:find("⟦ATTR⟧", 1, true)
  if cut then txt = txt:sub(1, cut - 1) end
  local first = txt:match("^(.-[%.%?!])%s") or txt
  first = first:gsub("^%s+", ""):gsub("%s+$", ""):gsub('"', "'")
  if #first > 220 then first = first:sub(1, 217) .. "..." end
  return first
end

local function figure_to_para(fig)
  local img
  pandoc.walk_block(pandoc.Div(fig.content), {
    Image = function(i) if not img then img = i end end
  })
  if not img then return nil end
  img.caption = pandoc.utils.blocks_to_inlines(fig.caption.long)
  if #img.caption > 0 then img.attributes["fig-alt"] = alt_from_caption(img.caption) end
  img.identifier = fig.identifier
  img.title = ""
  return pandoc.Para({ img })
end

function Figure(fig) return figure_to_para(fig) end

local function add_class_to_images(block, cls)
  return pandoc.walk_block(block, {
    Image = function(i) i.classes:insert(cls); return i end
  })
end

local function hoist_table_ids(blocks)
  local out = pandoc.List()
  for _, b in ipairs(blocks) do
    if b.t == "Div" and b.identifier:match("^tbl%-") then
      local inner = hoist_table_ids(b.content)
      local placed = false
      for i, ib in ipairs(inner) do
        if ib.t == "Table" and not placed then
          local long = ib.caption.long
          if #long > 0 and (long[#long].t == "Plain" or long[#long].t == "Para") then
            long[#long].content:insert(pandoc.RawInline("markdown", " {#" .. b.identifier .. "}"))
            ib.caption.long = long
            inner[i] = ib
          else
            io.stderr:write("table without caption: " .. b.identifier .. "\n")
          end
          placed = true
        end
      end
      out:extend(inner)
    else
      out:insert(b)
    end
  end
  return out
end

function Div(el)
  local cls = el.classes
  if cls:includes("center") then return el.content end
  if cls:includes("marginfigure") or cls:includes("widefigure") then
    local place = cls:includes("marginfigure") and "column-margin" or "column-page-right"
    local out = pandoc.List()
    for _, b in ipairs(el.content) do
      if b.t == "Figure" then b = figure_to_para(b) end
      if b then out:insert(add_class_to_images(b, place)) end
    end
    return out
  end
  if cls:includes("margintbl") or cls:includes("widetbl") then
    local place = cls:includes("margintbl") and "column-margin" or "column-page-right"
    return pandoc.Div(hoist_table_ids(el.content), pandoc.Attr("", { place }))
  end
  if el.identifier:match("^tbl%-") then return hoist_table_ids({ el }) end
  if cls:includes("question") then
    local id = ""
    local new = pandoc.List()
    for _, b in ipairs(el.content) do
      if id == "" and (b.t == "Para" or b.t == "Plain") then
        local inl = pandoc.List()
        for _, i in ipairs(b.content) do
          if id == "" and i.t == "Span" and i.identifier:match("^exr%-") and #i.content == 0 then
            id = i.identifier
          else
            inl:insert(i)
          end
        end
        b.content = inl
      end
      new:insert(b)
    end
    return pandoc.Div(new, pandoc.Attr(id, {}))
  end
  if cls:includes("ChapterSummary") then
    -- not a Quarto callout: callouts need the fontawesome5 LaTeX package in PDF output
    local blocks = pandoc.List({ pandoc.Para({ pandoc.Strong({ pandoc.Str("Chapter"), pandoc.Space(), pandoc.Str("summary.") }) }) })
    blocks:extend(el.content)
    return pandoc.Div(blocks, pandoc.Attr("", { "chapter-summary" }))
  end
end


-- Data tables need header cells (WCAG 1.3.1): LaTeX tabulars carry no header
-- information, so treat the first row as the header when pandoc found none.
function Table(tbl)
  if #tbl.head.rows == 0 and #tbl.bodies >= 1 and #tbl.bodies[1].body >= 2 then
    local first = tbl.bodies[1].body[1]
    tbl.bodies[1].body:remove(1)
    tbl.head.rows:insert(first)
    return tbl
  end
end

-- \begin{description} becomes a bullet list (Quarto's margin layout breaks <dl> markup)
function DefinitionList(dl)
  local items = pandoc.List()
  for _, item in ipairs(dl.content) do
    local blocks = pandoc.List({ pandoc.Plain({ pandoc.Strong(item[1]) }) })
    for _, def in ipairs(item[2]) do blocks:extend(def) end
    items:insert(blocks)
  end
  return pandoc.BulletList(items)
end
