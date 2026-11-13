// Resume in Typst — resume-forge visual language, tightened only when a
// one-page render needs it. Same sections and hierarchy as template.typ.

#let d = json("data.json")
#let st = d.at("style", default: (:))
#let fs = st.at("font_size", default: 10) * 1pt
#let leading = st.at("leading", default: 0.28)
#let secgap = st.at("section_gap", default: 8) * 1pt
#let entrygap = st.at("entry_gap", default: 5) * 1pt
#let bulletgap = st.at("bullet_gap", default: 2) * 1pt
#let has(x) = x != none and x != ""
#set document(title: d.name + " Resume")
#set page(
  paper: "us-letter",
  margin: (top: 0.08in, bottom: 0.08in, left: 0.18in, right: 0.18in),
)
#set text(font: "New Computer Modern", size: fs)
#set par(justify: false, leading: leading * 1em, spacing: 0.45em)

#let section(title) = block(above: secgap, below: 3pt, {
  text(weight: "regular", size: fs - 0.5pt, smallcaps(title))
  v(1.5pt)
  line(length: 100%, stroke: 0.45pt)
})

#let subheading(title, date, subtitle, location) = block(
  above: entrygap,
  below: 0pt,
  grid(
    columns: (1fr, auto),
    align: (left + horizon, right + horizon),
    row-gutter: 1pt,
    text(weight: "bold", title),
    text(date),
    text(style: "italic", size: fs - 1.7pt, subtitle),
    text(style: "italic", size: fs - 1.7pt, location),
  ),
)

#let project-heading(p) = block(above: entrygap, below: 0pt, text(size: fs - 0.8pt)[
  #strong(p.name)
  #if has(p.tech_line) [ | #emph(p.tech_line) ]
  #if has(p.url) [ | #link(p.url)[GitHub] ]
])

#let items(..values) = block(above: 0pt, below: 0pt, {
  for item in values.pos() {
    grid(
      columns: (7pt, 1fr),
      align: (right + top, left + top),
      text(size: fs - 1.7pt, "•"),
      text(size: fs - 1.7pt, item),
    )
    v(bulletgap)
  }
})

#let daterange(e) = if has(e.start) and has(e.end) {
  e.start + " - " + e.end
} else if has(e.end) {
  e.end
} else {
  ""
}

// Header
#align(center)[
  #text(weight: "bold", size: fs + 8pt, smallcaps(d.name))
  #v(-3pt)
  #text(size: fs - 0.5pt)[
    #for (i, item) in d.header.enumerate() [
      #if i > 0 [ | ]
      #if has(item.url) [#link(item.url)[#underline[#item.text]]] else [#item.text]
    ]
  ]
]

// Summary
#if has(d.summary) [
  #section("Summary")
  #text(size: fs - 1pt, d.summary)
]

// Education
#if d.education.len() > 0 [
  #section("Education")
  #for e in d.education [
    #subheading(e.institution, daterange(e), e.degree_line, e.location)
    #v(6pt, weak: false)
    #if e.courses.len() > 0 [
      #items([Relevant Courses: #e.courses.join(", ")])
    ]
  ]
]

// Experience
#if d.experience.len() > 0 [
  #section("Experience")
  #for e in d.experience [
    #subheading(e.company, daterange(e), e.title, e.location)
    #v(6pt, weak: false)
    #if e.bullets.len() > 0 [
      #items(..e.bullets)
    ]
  ]
]

// Projects
#if d.projects.len() > 0 [
  #section("Projects")
  #for p in d.projects [
    #project-heading(p)
    #v(6pt, weak: false)
    #if p.bullets.len() > 0 [
      #items(..p.bullets)
    ]
  ]
]

// Technical Skills
#if d.skill_groups.len() > 0 [
  #section("Technical Skills")
  #set par(spacing: 0.22em)
  #text(size: fs - 2.8pt)[
    #for group in d.skill_groups [
      #strong(group.label): #group.items.join(", ")
      #linebreak()
    ]
  ]
]
