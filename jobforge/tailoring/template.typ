#set page(
  paper: "us-letter",
  margin: (top: 0.42in, bottom: 0.42in, left: 0.55in, right: 0.55in),
)
#set text(font: "New Computer Modern", size: 10pt, fill: black)
#set par(leading: 0.42em, spacing: 0.55em, justify: false)

#let d = json("data.json")
#let has(x) = x != none and x != ""
#let contact = d.contact.filter(x => has(x)).join(" | ")
#let daterange(e) = if has(e.start) and has(e.end) {
  e.start + " -- " + e.end
} else if has(e.end) {
  e.end
} else {
  ""
}
#let section(title) = block(width: 100%, above: 0.7em, below: 0.3em)[
  #text(10.5pt, weight: "bold", upper(title))
  #v(-0.12em)
  #line(length: 100%, stroke: 0.6pt + black)
]

#align(center)[
  #text(17pt, weight: "bold")[#d.name]
  #linebreak()
  #text(9pt)[#contact]
]

#if has(d.summary) [
  #section("Summary")
  #d.summary
]

#if d.experience.len() > 0 [
  #section("Experience")
  #for e in d.experience [
    #grid(
      columns: (1fr, auto),
      column-gutter: 0.5em,
      [#text(weight: "bold")[#e.title]#if has(e.company) [ — #e.company]],
      [#align(right)[#daterange(e)]],
    )
    #if has(e.location) [
      #text(style: "italic", size: 9pt)[#e.location]
    ]
    #for b in e.bullets [
      - #b
    ]
    #v(0.25em)
  ]
]

#if d.projects.len() > 0 [
  #section("Projects")
  #for p in d.projects [
    #text(weight: "bold")[#p.name]#if has(p.description) [ — #p.description]
    #for b in p.bullets [
      - #b
    ]
    #v(0.25em)
  ]
]

#if d.education.len() > 0 [
  #section("Education")
  #for e in d.education [
    #grid(
      columns: (1fr, auto),
      column-gutter: 0.5em,
      [
        #text(weight: "bold")[#e.institution]
        #if has(e.degree) [ — #e.degree]
        #if has(e.major) [#emph(e.major)]
      ],
      [#align(right)[#daterange(e)]],
    )
    #v(0.15em)
  ]
]

#if d.certifications.len() > 0 [
  #section("Certifications")
  #for c in d.certifications [
    - #c
  ]
]

#if d.skills.len() > 0 [
  #section("Skills")
  #d.skills.join(", ")
]
