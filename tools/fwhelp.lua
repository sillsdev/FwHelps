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
  -- RoboHelp table presentation classes carry no document semantics.
  hcp1          = "plain",
  hcp2          = "plain",
  hcp3          = "plain",
  hcp4          = "plain",
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

local function uri_kind(target)
  local path = tostring(target or "")
  path = path:gsub("%%([0-9A-Fa-f][0-9A-Fa-f])", function(hex)
    return string.char(tonumber(hex, 16))
  end)
  path = path:gsub("\\", "/")
  path = path:gsub("[%z\001-\032]", "")
  path = path:gsub("#.*$", "")
  if path == "" then return "fragment" end
  if path:sub(1, 1) == "/" or path:match("^[A-Za-z]:/") then return "path_escape" end
  local scheme = path:match("^([A-Za-z][A-Za-z0-9+%%.-]*):")
  if scheme then
    scheme = scheme:lower()
    if scheme == "http" or scheme == "https" or scheme == "mailto" then return "external" end
    return "unsafe_uri"
  end
  return "local"
end

local function neutralize_uri(target)
  local kind = uri_kind(target)
  if kind == "unsafe_uri" or kind == "path_escape" then
    io.stderr:write("FWHELP_" .. kind:upper() .. " " .. tostring(target) .. "\n")
    return "#"
  end
  return target
end

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
  -- Inventory every authored class before applying a supported transform.
  -- A span may carry both a supported class and an unknown semantic.
  for _, cls in ipairs(el.classes) do
    if SPAN_MAP[cls] == nil then
      unmapped[cls] = (unmapped[cls] or 0) + 1
    end
  end
  for _, cls in ipairs(el.classes) do
    local kind = SPAN_MAP[cls]
    if kind == "strong" then return pandoc.Strong(el.content)
    elseif kind == "emph" then return pandoc.Emph(el.content)
    elseif kind == "code" then return pandoc.Code(text_of(el.content))
    elseif kind == "superscript" then return pandoc.Superscript(el.content)
    elseif kind == "plain" then return el.content
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
  local kind = uri_kind(t)
  if kind == "unsafe_uri" or kind == "path_escape" then
    el.target = neutralize_uri(t)
    return el
  end
  if kind == "fragment" or kind == "external" then return el end
  local path, frag = t:match("^([^#]*)(.*)$")
  local rewritten, n = path:gsub("%.html?$", ".md")
  if n > 0 then el.target = rewritten .. frag end
  return el
end

--- Drop the presentational attributes RoboHelp puts on every image.
--- GFM cannot express width/height/style, so pandoc falls back to a raw <img>
--- tag and 2,109 of them were surviving into the markdown across 842 files.
--- The sizes are RoboHelp's inline-icon dimensions, not information.
function Image(el)
  -- Decorative RoboHelp marker on Tip/Note headings. Empty-alt images
  -- stringify as U+FFFD in alert detection and GFM output.
  local src = tostring(el.src or "")
  if src == "" and el.target then src = tostring(el.target) end
  if src:lower():match("note[_-]?icon%.gif") then
    return pandoc.List()
  end
  el.src = neutralize_uri(src)
  el.attr = pandoc.Attr()
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

local function alert_kind(block)
  if block.t ~= "Header" then return nil end
  for _, cls in ipairs(block.classes) do
    if ALERT_MAP[cls] then return ALERT_MAP[cls] end
  end
  -- Some callouts carry the word as heading text with no class.
  local t = text_of(block.content):gsub("^%s*[^%w]*%s*", "")
  return ALERT_MAP[t]
end

--- Wrap Note/Tip/Important/Warning/Caution callouts in GitHub alert
--- blockquotes. This has to happen here rather than in Header() because the
--- callout body is the *following* blocks, not the heading's children.
function Blocks(blocks)
  local out = pandoc.List()
  local i = 1
  while i <= #blocks do
    local b = blocks[i]

    -- The "Related Topics" / "Related Internet Sites" trailers stay where the
    -- author put them. Reconstructing them from link labels alone lost the
    -- prose between the links -- "Lists overview (task helps)" became "Lists
    -- overview", and "Choose a translation type (in an Example Lexicon Edit)"
    -- lost both its qualifier and its second link. Python only normalises the
    -- heading level afterwards.
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

    -- A handful of RoboHelp list fragments arrive as literal text beginning
    -- "- -" instead of a nested list node. Reparse only that malformed
    -- marker through Pandoc's Markdown reader so the item remains content but
    -- is emitted as a real nested list (never a literal marker).
    if b.t == "Para" and text_of(b.content):match("^%s*%-%s+%-%s+") then
      local repaired = pandoc.read(text_of(b.content), "markdown")
      for _, item in ipairs(repaired.blocks) do out:insert(item) end
    else
      out:insert(b)
    end
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

-- Only the functions named here run: returning an explicit filter list opts out
-- of pandoc's pick-up-every-global behaviour, so a handler left off this table
-- is silently dead code.
-- Span/Table/Div/Header run before Blocks so trailers are detected on clean text.
return {
  { Span = Span, Table = Table, Div = Div, Header = Header, Link = Link,
    Image = Image },
  { Blocks = Blocks },
  { Pandoc = Pandoc },
}
