// Resume in Typst — resume-forge visual language.
// Data comes from data.json; the layout mirrors the refined base resume.

#let d = json("data.json")
#let has(x) = x != none and x != ""
#set document(title: d.name + " Resume")
#set page(
  paper: "us-letter",
  margin: (top: 0.12in, bottom: 0.12in, left: 0.2in, right: 0.2in),
)
#set text(font: "New Computer Modern", size: 11pt)
#set par(justify: false, leading: 0.28em)

#let section(title) = {
  v(6pt)
  text(weight: "regular", size: 10pt, smallcaps(title))
  v(-9pt)
  line(length: 100%, stroke: 0.5pt)
  v(-5pt)
}

#let subheading(title, date, subtitle, location) = {
  v(5pt)
  grid(
    columns: (1fr, auto),
    align: (left, right),
    text(weight: "bold", title),
    text(date),
  )
  v(-7pt)
  grid(
    columns: (1fr, auto),
    align: (left, right),
    text(style: "italic", size: 9pt, subtitle),
    text(style: "italic", size: 9pt, location),
  )
  v(-2pt)
}

#let project-heading(p) = {
  v(4pt)
  text(size: 9.5pt)[
    #strong(p.name)
    #if has(p.tech_line) [ | #emph(p.tech_line) ]
    #if has(p.url) [ | #link(p.url)[GitHub] ]
  ]
  v(-2pt)
}

#let items(..values) = {
  v(-3pt)
  for item in values.pos() {
    grid(
      columns: (7pt, 1fr),
      align: (right, left),
      text(size: 9pt, "•"),
      h(2pt) + text(size: 9pt, item),
    )
    v(-3.5pt)
  }
  v(-3pt)
}

#let daterange(e) = if has(e.start) and has(e.end) {
  e.start + " - " + e.end
} else if has(e.end) {
  e.end
} else {
  ""
}

// Header
#align(center)[
  #text(weight: "bold", size: 20pt, smallcaps(d.name))
  #v(-5pt)
  #text(size: 10pt)[
    #for (i, item) in d.header.enumerate() [
      #if i > 0 [ | ]
      #if has(item.url) [#link(item.url)[#underline[#item.text]]] else [#item.text]
    ]
  ]
]

// Education
#if d.education.len() > 0 [
  #section("Education")
  #for e in d.education [
    #subheading(e.institution, daterange(e), e.degree_line, e.location)
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
    #if p.bullets.len() > 0 [
      #items(..p.bullets)
    ]
  ]
]

// Technical Skills
#if d.skill_groups.len() > 0 [
  #section("Technical Skills")
  #v(-3pt)
  #set par(spacing: 0.22em)
  #text(size: 8pt)[
    #for group in d.skill_groups [
      #strong(group.label): #group.items.join(", ")
      #linebreak()
    ]
  ]
]
