// Resume in Typst — resume-forge visual language, tightened only when a
// one-page render needs it. Same sections and hierarchy as template.typ.

#let d = json("data.json")
#let has(x) = x != none and x != ""
#set document(title: d.name + " Resume")
#set page(
  paper: "us-letter",
  margin: (top: 0.08in, bottom: 0.08in, left: 0.18in, right: 0.18in),
)
#set text(font: "New Computer Modern", size: 10pt)
#set par(justify: false, leading: 0.20em)

#let section(title) = {
  v(3pt)
  text(weight: "regular", size: 9.5pt, smallcaps(title))
  v(-5pt)
  line(length: 100%, stroke: 0.45pt)
  v(-1pt)
}

#let subheading(title, date, subtitle, location) = {
  v(2pt)
  grid(
    columns: (1fr, auto),
    align: (left, right),
    text(weight: "bold", title),
    text(date),
  )
  v(-4pt)
  grid(
    columns: (1fr, auto),
    align: (left, right),
    text(style: "italic", size: 8.3pt, subtitle),
    text(style: "italic", size: 8.3pt, location),
  )
  v(-1pt)
}

#let project-heading(p) = {
  v(3pt)
  text(size: 9.2pt)[
    #strong(p.name)
    #if has(p.tech_line) [ | #emph(p.tech_line) ]
    #if has(p.url) [ | #link(p.url)[GitHub] ]
  ]
  v(-1pt)
}

#let items(..values) = {
  v(-1pt)
  for item in values.pos() {
    grid(
      columns: (7pt, 1fr),
      align: (right, left),
      text(size: 8.3pt, "•"),
      h(2pt) + text(size: 8.3pt, item),
    )
    v(-2pt)
  }
  v(-1pt)
}

#let daterange(e) = if has(e.start) and has(e.end) {
  e.start + " - " + e.end
} else if has(e.end) {
  e.end
} else {
  ""
}

#align(center)[
  #text(weight: "bold", size: 18pt, smallcaps(d.name))
  #v(-5pt)
  #text(size: 9.5pt)[
    #for (i, item) in d.header.enumerate() [
      #if i > 0 [ | ]
      #if has(item.url) [#link(item.url)[#underline[#item.text]]] else [#item.text]
    ]
  ]
]

#if d.education.len() > 0 [
  #section("Education")
  #for e in d.education [
    #subheading(e.institution, daterange(e), e.degree_line, e.location)
    #if e.courses.len() > 0 [
      #items([Relevant Courses: #e.courses.join(", ")])
    ]
  ]
]

#if d.experience.len() > 0 [
  #section("Experience")
  #for e in d.experience [
    #subheading(e.company, daterange(e), e.title, e.location)
    #if e.bullets.len() > 0 [
      #items(..e.bullets)
    ]
  ]
]

#if d.projects.len() > 0 [
  #section("Projects")
  #for p in d.projects [
    #project-heading(p)
    #if p.bullets.len() > 0 [
      #items(..p.bullets)
    ]
  ]
]

#if d.skill_groups.len() > 0 [
  #section("Technical Skills")
  #v(-2pt)
  #set par(spacing: 0.12em)
  #text(size: 7.2pt)[
    #for group in d.skill_groups [
      #strong(group.label): #group.items.join(", ")
      #linebreak()
    ]
  ]
]
