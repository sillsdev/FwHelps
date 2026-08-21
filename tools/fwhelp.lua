-- Pandoc Lua filter: RoboHelp XHTML -> clean GFM for humans and RAG.
--
-- Runs inside pandoc (Lua 5.4 is bundled; no separate install). Everything here
-- needs the document tree, which is why it lives in Lua rather than Python.
--
-- Unmapped span classes are collected and reported so the build can fail loudly
-- rather than silently dropping a semantic distinction we did not know about.

local unmapped = {}

-- RoboHelp's authored character styles. Anything not listed is a build error.
local SPAN_MAP = {
  UserInterface = "strong",  -- 18708x: menu/button/field names
  Strong        = "strong",
  Emphasis      = "emph",
  DefinedWord   = "emph",
  BookTitle     = "emph",
  VernacularWord = "emph",   -- example-language data, NOT English prose
  TypedText     = "code",    -- literal text the user types
  Keyboard      = "code",    -- key names
  FileName      = "code",
  Filename      = "code",    -- authoring typo, same intent
  Placeholder   = "emph",
  Superscript   = "superscript",
  nobr          = "plain",
  expandtext    = "plain",
  ["Strong\""]  = "strong",  -- malformed class attribute in source
}

-- h4 callout classes -> GitHub alert blocks
local ALERT_MAP = {
  Note = "NOTE", Tip = "TIP", Important = "IMPORTANT",
  Warning = "WARNING", Caution = "CAUTION",
}

local function text_of(inlines)
  -- gsub returns (string, count); returning it directly would pass the count
  -- as pandoc.Code's second argument, which it reads as an Attr.
  local s = pandoc.utils.stringify(inlines)
  s = s:gsub("^%s+", "")
  s = s:gsub("%s+$", "")
  return s
end

--- Collapse authored character styles into real markdown emphasis.
function Span(el)
  if #el.classes == 0 then return el.content end
  for _, cls in ipairs(el.classes) do
    local kind = SPAN_MAP[cls]
    if kind == "strong" then return pandoc.Strong(el.content)
    elseif kind == "emph" then return pandoc.Emph(el.content)
    elseif kind == "code" then return pandoc.Code(text_of(el.content))
    elseif kind == "superscript" then return pandoc.Superscript(el.content)
    elseif kind == "plain" then return el.content
    elseif kind == nil then
      unmapped[cls] = (unmapped[cls] or 0) + 1
    end
  end
  return el.content
end

--- Repoint internal topic links at their .md counterparts.
--- Done here rather than with a regex over the rendered markdown because
--- filenames like "Export_full_lexicon_(LIFT).htm" contain parentheses, which
--- no sane link regex survives. The AST has the target as a plain string.
function Link(el)
  local t = el.target
  if t == "" or t:sub(1, 1) == "#" or t:match("^%a[%w+.%-]*:") then return el end
  local path, frag = t:match("^([^#]*)(.*)$")
  local rewritten, n = path:gsub("%.html?$", ".md")
  if n > 0 then el.target = rewritten .. frag end
  return el
end

--- Collect every row across head/bodies/foot as a flat list.
local function all_rows(el)
  local rows = pandoc.List()
  for _, r in ipairs(el.head.rows) do rows:insert(r) end
  for _, b in ipairs(el.bodies) do
    for _, r in ipairs(b.head) do rows:insert(r) end
    for _, r in ipairs(b.body) do rows:insert(r) end
  end
  for _, r in ipairs(el.foot.rows) do rows:insert(r) end
  return rows
end

--- Is this a 2-column "label:/value" layout rather than real tabular data?
--- 560 of 769 tables in the corpus are these -- "Full name:", "Location:",
--- "Description:", "Field type:" -- i.e. definition lists that RoboHelp
--- happened to render with <table>. 79% of tables carry block content in a
--- cell, so pandoc must fall back to raw HTML for them; turning them into
--- prose removes almost all remaining HTML from the output.
local function is_definition_table(rows)
  if #rows == 0 then return false end
  local labelish = 0
  for _, row in ipairs(rows) do
    if #row.cells ~= 2 then return false end
    local label = text_of(row.cells[1].contents)
    if label:sub(-1) == ":" and #label < 40 then labelish = labelish + 1 end
  end
  return labelish / #rows >= 0.8
end

function Table(el)
  local rows = all_rows(el)

  if is_definition_table(rows) then
    local out = pandoc.List()
    for _, row in ipairs(rows) do
      local label = text_of(row.cells[1].contents):gsub(":%s*$", "")
      local value = row.cells[2].contents
      local head = pandoc.List({ pandoc.Strong(pandoc.Str(label .. ":")) })
      -- Fold a single-paragraph value onto the label line; otherwise keep the
      -- label on its own line and let lists/multiple paragraphs follow.
      if #value == 1 and value[1].t == "Para" then
        head:insert(pandoc.Space())
        head:extend(value[1].content)
        out:insert(pandoc.Para(head))
      else
        out:insert(pandoc.Para(head))
        out:extend(value)
      end
    end
    return out
  end

  -- Genuine data table: drop RoboHelp's fixed column widths so it emits as a
  -- GFM pipe table instead of a grid table (11,670 inline width: styles).
  for i, spec in ipairs(el.colspecs) do
    el.colspecs[i] = { spec[1], nil }
  end
  return el
end

--- Strip presentational wrappers pandoc lifts from RoboHelp's <div>/<font>.
function Div(el)
  -- el.attributes is an AttributeList, not a plain table, so `next` fails on it.
  if #el.classes == 0 and el.identifier == "" then return el.content end
  return el
end

--- Every topic opens with an <h2> title, which Python promotes to the page's
--- single h1. Shift body headings up to match, leaving the 4 stray h1s alone.
function Header(el)
  if el.level > 1 then el.level = el.level - 1 end
  return el
end

local TRAILER = { ["Related Topics"] = true, ["Related Internet Sites"] = true }

local function alert_kind(block)
  if block.t ~= "Header" then return nil end
  for _, cls in ipairs(block.classes) do
    if ALERT_MAP[cls] then return ALERT_MAP[cls] end
  end
  -- Some callouts carry the word as heading text with no class.
  local t = text_of(block.content):gsub("^%s*[^%w]*%s*", "")
  return ALERT_MAP[t]
end

--- Two block-level jobs in one pass:
---
--- 1. Strip the "Related Topics" / "Related Internet Sites" trailers. 1,574 of
---    1,599 topics carry one; as prose they append a link list to nearly every
---    chunk. Python re-attaches them as frontmatter, so they survive as a link
---    graph without polluting the embedded text.
---
--- 2. Wrap Note/Tip/Important/Warning/Caution callouts in GitHub alert
---    blockquotes. This has to happen here rather than in Header() because the
---    callout body is the *following* blocks, not the heading's children.
function Blocks(blocks)
  local out = pandoc.List()
  local i = 1
  while i <= #blocks do
    local b = blocks[i]

    if b.t == "Header" and TRAILER[text_of(b.content)] then
      local level = b.level
      i = i + 1
      while i <= #blocks and not (blocks[i].t == "Header" and blocks[i].level <= level) do
        i = i + 1
      end
      goto continue
    end

    local kind = alert_kind(b)
    if kind then
      -- Raw, not Str: pandoc would escape the brackets to "\[!NOTE\]", which
      -- GitHub no longer recognises as an alert.
      local marker = pandoc.RawInline("gfm", "[!" .. kind .. "]")
      local body = pandoc.List({ pandoc.Para({ marker }) })
      local level = b.level
      i = i + 1
      while i <= #blocks and not (blocks[i].t == "Header" and blocks[i].level <= level) do
        body:insert(blocks[i])
        i = i + 1
      end
      out:insert(pandoc.BlockQuote(body))
      goto continue
    end

    out:insert(b)
    i = i + 1
    ::continue::
  end
  return out
end

--- Emit unmapped classes on stderr for the build to pick up.
function Pandoc(doc)
  local names = {}
  for cls, n in pairs(unmapped) do names[#names + 1] = cls .. "=" .. n end
  if #names > 0 then
    io.stderr:write("FWHELP_UNMAPPED_SPAN " .. table.concat(names, ",") .. "\n")
  end
  return doc
end

-- Span/Table/Div/Header run before Blocks so trailers are detected on clean text.
return {
  { Span = Span, Table = Table, Div = Div, Header = Header },
  { Blocks = Blocks },
  { Pandoc = Pandoc },
}
