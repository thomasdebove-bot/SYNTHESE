# -*- coding: utf-8 -*-
"""
Script pyRevit : détection de conflits entre maquettes liées + navigation des conflits.

Fonctionnalités :
1) Détecte des conflits géométriques entre éléments de liens Revit.
2) Génère une vue 3D "coupe" (section box) par conflit.
3) Ouvre une fenêtre Windows Forms pour faire défiler les conflits.
4) Affiche des propositions de contournement selon les catégories en conflit.

Exécution : depuis pyRevit (bouton script) avec un document hôte ouvert.
"""

from __future__ import division, print_function

import clr

clr.AddReference("System")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from System.Drawing import Point, Size
from System.Windows.Forms import (
    Button,
    DataGridView,
    DataGridViewAutoSizeColumnsMode,
    DataGridViewSelectionMode,
    Form,
    Label,
    MessageBox,
    RichTextBox,
)

clr.AddReference("RevitAPI")
clr.AddReference("RevitServices")
from RevitServices.Persistence import DocumentManager

from Autodesk.Revit.DB import (
    BoundingBoxXYZ,
    BooleanOperationsType,
    BooleanOperationsUtils,
    BuiltInCategory,
    FilteredElementCollector,
    Options,
    Outline,
    RevitLinkInstance,
    Solid,
    SolidUtils,
    Transform,
    Transaction,
    View3D,
    ViewFamily,
    ViewFamilyType,
    ViewDuplicateOption,
    XYZ,
)

def get_revit_context():
    """
    Compatibilité pyRevit + Dynamo.
    - pyRevit: utilise __revit__.ActiveUIDocument
    - Dynamo: utilise DocumentManager.Instance.CurrentDBDocument
    """
    # pyRevit
    try:
        ui = __revit__.ActiveUIDocument  # noqa: F821 (variable injectée par pyRevit)
        if ui and ui.Document:
            return ui, ui.Document, "pyrevit"
    except Exception:
        pass

    # Dynamo / RevitServices
    try:
        doc = DocumentManager.Instance.CurrentDBDocument
        uiapp = DocumentManager.Instance.CurrentUIApplication
        ui = uiapp.ActiveUIDocument if uiapp else None
        if doc:
            return ui, doc, "dynamo"
    except Exception:
        pass

    raise Exception(
        "Impossible de récupérer le contexte Revit. "
        "Lancez ce script depuis pyRevit ou Dynamo dans Revit."
    )


uidoc, DOC, CONTEXT = get_revit_context()


TARGET_CATEGORIES = [
    BuiltInCategory.OST_Walls,
    BuiltInCategory.OST_Floors,
    BuiltInCategory.OST_StructuralFraming,
    BuiltInCategory.OST_StructuralColumns,
    BuiltInCategory.OST_PipeCurves,
    BuiltInCategory.OST_DuctCurves,
    BuiltInCategory.OST_CableTray,
    BuiltInCategory.OST_Conduit,
    BuiltInCategory.OST_PlumbingFixtures,
    BuiltInCategory.OST_MechanicalEquipment,
]


def get_solid_from_element(element):
    """Retourne le plus grand solide valide trouvé dans la géométrie de l'élément."""
    opts = Options()
    geom = element.get_Geometry(opts)
    if not geom:
        return None

    best = None
    best_vol = 0.0

    for g in geom:
        if isinstance(g, Solid) and g.Volume > best_vol and g.Faces.Size > 0:
            best = g
            best_vol = g.Volume
        elif hasattr(g, "GetInstanceGeometry"):
            inst_geom = g.GetInstanceGeometry()
            for ig in inst_geom:
                if isinstance(ig, Solid) and ig.Volume > best_vol and ig.Faces.Size > 0:
                    best = ig
                    best_vol = ig.Volume

    return best


def transformed_outline(solid, trf):
    """Crée un outline à partir de la bbox du solide transformé."""
    bb = solid.GetBoundingBox()
    mins = trf.OfPoint(bb.Min)
    maxs = trf.OfPoint(bb.Max)
    min_pt = XYZ(min(mins.X, maxs.X), min(mins.Y, maxs.Y), min(mins.Z, maxs.Z))
    max_pt = XYZ(max(mins.X, maxs.X), max(mins.Y, maxs.Y), max(mins.Z, maxs.Z))
    return Outline(min_pt, max_pt)


def midpoint_xyz(a, b):
    """Calcule le milieu entre 2 XYZ sans opérateurs Python (+, /) non supportés partout."""
    return XYZ(
        (a.X + b.X) / 2.0,
        (a.Y + b.Y) / 2.0,
        (a.Z + b.Z) / 2.0,
    )


def get_workaround_suggestion(cat_a, cat_b):
    """Suggestions métiers basées sur le couple de catégories."""
    key = tuple(sorted([cat_a, cat_b]))

    suggestions = {
        tuple(sorted(["Ducts", "Structural Framing"])): (
            "Décaler la gaine, vérifier les réservations structurelles, "
            "ou remplacer par une gaine plate localement."
        ),
        tuple(sorted(["Pipes", "Structural Framing"])): (
            "Re-router la tuyauterie avec coudes doux, ou créer une réservation validée BET structure."
        ),
        tuple(sorted(["Cable Trays", "Ducts"])): (
            "Prioriser la gaine principale, puis décaler le chemin de câble en altitude."
        ),
        tuple(sorted(["Walls", "Pipes"])): (
            "Créer une réservation dans le mur + manchon coupe-feu si requis."
        ),
    }

    return suggestions.get(
        key,
        "Analyser le conflit avec les disciplines concernées, ajuster altimétrie/trajectoire, "
        "puis valider les impacts maintenance et normes incendie.",
    )


def get_or_create_base_3d_view(doc):
    """Récupère une vue 3D non-template ou en crée une."""
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if not v.IsTemplate:
            return v

    vft = (
        FilteredElementCollector(doc)
        .OfClass(ViewFamilyType)
        .ToElements()
    )
    vft = [x for x in vft if x.ViewFamily == ViewFamily.ThreeDimensional]
    if not vft:
        raise Exception("Aucun type de vue 3D disponible.")

    with Transaction(doc, "Créer vue 3D base") as t:
        t.Start()
        view = View3D.CreateIsometric(doc, vft[0].Id)
        view.Name = "_Clash_Base_3D"
        t.Commit()
    return view


def create_section_view_for_conflict(doc, base_view, clash, idx):
    """Crée une vue 3D du conflit avec section box."""
    name = "CLASH_{0:03d}_{1}_{2}".format(idx + 1, clash["link_a"], clash["link_b"])

    with Transaction(doc, "Créer coupe conflit") as t:
        t.Start()
        new_view_id = base_view.Duplicate(ViewDuplicateOption.Duplicate)
        new_view = doc.GetElement(new_view_id)
        new_view.Name = name[:120]

        center = clash["center"]
        half = clash["half_size"]

        sec = BoundingBoxXYZ()
        sec.Min = XYZ(center.X - half, center.Y - half, center.Z - half)
        sec.Max = XYZ(center.X + half, center.Y + half, center.Z + half)
        sec.Transform = Transform.Identity

        new_view.SetSectionBox(sec)
        new_view.IsSectionBoxActive = True
        t.Commit()

    return new_view


def detect_clashes(doc):
    """Détecte les conflits entre maquettes liées."""
    links = list(FilteredElementCollector(doc).OfClass(RevitLinkInstance))
    if len(links) < 2:
        return [], {"boolean_failures": 0}

    link_data = []
    for li in links:
        ldoc = li.GetLinkDocument()
        if ldoc is None:
            continue
        link_data.append((li, ldoc, li.GetTotalTransform()))

    clashes = []
    checked_pairs = set()
    boolean_failures = 0

    for i in range(len(link_data)):
        inst_a, doc_a, trf_a = link_data[i]
        elems_a = []
        for bic in TARGET_CATEGORIES:
            elems_a.extend(
                list(
                    FilteredElementCollector(doc_a)
                    .OfCategory(bic)
                    .WhereElementIsNotElementType()
                    .ToElements()
                )
            )

        for j in range(i + 1, len(link_data)):
            inst_b, doc_b, trf_b = link_data[j]

            pair_key = tuple(sorted([inst_a.Id.IntegerValue, inst_b.Id.IntegerValue]))
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            elems_b = []
            for bic in TARGET_CATEGORIES:
                elems_b.extend(
                    list(
                        FilteredElementCollector(doc_b)
                        .OfCategory(bic)
                        .WhereElementIsNotElementType()
                        .ToElements()
                    )
                )

            solids_b = []
            for eb in elems_b:
                sb = get_solid_from_element(eb)
                if not sb:
                    continue
                solids_b.append((eb, sb, transformed_outline(sb, trf_b)))

            for ea in elems_a:
                sa = get_solid_from_element(ea)
                if not sa:
                    continue

                tsa = SolidUtils.CreateTransformed(sa, trf_a)
                oa = transformed_outline(sa, trf_a)

                for eb, sb, ob in solids_b:
                    if not oa.Intersects(ob, 0.01):
                        continue

                    tsb = SolidUtils.CreateTransformed(sb, trf_b)
                    try:
                        inter = BooleanOperationsUtils.ExecuteBooleanOperation(
                            tsa, tsb, BooleanOperationsType.Intersect
                        )
                    except Exception:
                        # Certains solides Revit sont invalides pour le booléen.
                        # On ignore la paire et on poursuit l'analyse globale.
                        boolean_failures += 1
                        continue

                    if inter and inter.Volume > 0.0001:
                        bb = inter.GetBoundingBox()
                        center = midpoint_xyz(bb.Min, bb.Max)
                        size_x = abs(bb.Max.X - bb.Min.X)
                        size_y = abs(bb.Max.Y - bb.Min.Y)
                        size_z = abs(bb.Max.Z - bb.Min.Z)
                        half_size = max(size_x, size_y, size_z, 2.0)

                        cat_a = ea.Category.Name if ea.Category else "Unknown"
                        cat_b = eb.Category.Name if eb.Category else "Unknown"

                        clashes.append(
                            {
                                "link_a": inst_a.Name,
                                "link_b": inst_b.Name,
                                "elem_a_id": ea.Id.IntegerValue,
                                "elem_b_id": eb.Id.IntegerValue,
                                "cat_a": cat_a,
                                "cat_b": cat_b,
                                "center": center,
                                "half_size": half_size,
                                "suggestion": get_workaround_suggestion(cat_a, cat_b),
                            }
                        )

    return clashes, {"boolean_failures": boolean_failures}


class ClashBrowser(Form):
    def __init__(self, doc, uidoc, clashes):
        Form.__init__(self)
        self.doc = doc
        self.uidoc = uidoc
        self.clashes = clashes
        self.index = 0
        self.views = {}

        self.Text = "Navigateur de conflits Revit"
        self.Size = Size(1100, 640)

        self.grid = DataGridView()
        self.grid.Location = Point(10, 10)
        self.grid.Size = Size(1060, 360)
        self.grid.ReadOnly = True
        self.grid.SelectionMode = DataGridViewSelectionMode.FullRowSelect
        self.grid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill

        self.lbl = Label()
        self.lbl.Location = Point(10, 380)
        self.lbl.Size = Size(1060, 24)

        self.suggestion = RichTextBox()
        self.suggestion.Location = Point(10, 410)
        self.suggestion.Size = Size(1060, 120)
        self.suggestion.ReadOnly = True

        self.btn_prev = Button(Text="Précédent")
        self.btn_prev.Location = Point(10, 545)
        self.btn_prev.Click += self.on_prev

        self.btn_next = Button(Text="Suivant")
        self.btn_next.Location = Point(120, 545)
        self.btn_next.Click += self.on_next

        self.btn_open = Button(Text="Ouvrir coupe")
        self.btn_open.Location = Point(230, 545)
        self.btn_open.Click += self.on_open_view

        self.btn_close = Button(Text="Fermer")
        self.btn_close.Location = Point(950, 545)
        self.btn_close.Click += self.on_close

        self.Controls.Add(self.grid)
        self.Controls.Add(self.lbl)
        self.Controls.Add(self.suggestion)
        self.Controls.Add(self.btn_prev)
        self.Controls.Add(self.btn_next)
        self.Controls.Add(self.btn_open)
        self.Controls.Add(self.btn_close)

        self.populate_grid()
        self.refresh_details()

    def populate_grid(self):
        self.grid.Columns.Add("id", "#")
        self.grid.Columns.Add("links", "Liens")
        self.grid.Columns.Add("elems", "Éléments")
        self.grid.Columns.Add("cats", "Catégories")

        for i, c in enumerate(self.clashes):
            self.grid.Rows.Add(
                str(i + 1),
                "{} <> {}".format(c["link_a"], c["link_b"]),
                "{} / {}".format(c["elem_a_id"], c["elem_b_id"]),
                "{} / {}".format(c["cat_a"], c["cat_b"]),
            )

    def refresh_details(self):
        if not self.clashes:
            self.lbl.Text = "Aucun conflit détecté"
            self.suggestion.Text = ""
            return

        c = self.clashes[self.index]
        self.lbl.Text = "Conflit {}/{} - {}({}) vs {}({})".format(
            self.index + 1,
            len(self.clashes),
            c["cat_a"],
            c["elem_a_id"],
            c["cat_b"],
            c["elem_b_id"],
        )
        self.suggestion.Text = c["suggestion"]
        self.grid.ClearSelection()
        self.grid.Rows[self.index].Selected = True

    def on_prev(self, sender, event):
        if not self.clashes:
            return
        self.index = (self.index - 1) % len(self.clashes)
        self.refresh_details()

    def on_next(self, sender, event):
        if not self.clashes:
            return
        self.index = (self.index + 1) % len(self.clashes)
        self.refresh_details()

    def on_open_view(self, sender, event):
        if not self.clashes or self.uidoc is None:
            return

        clash = self.clashes[self.index]
        if self.index not in self.views:
            base = get_or_create_base_3d_view(self.doc)
            view = create_section_view_for_conflict(self.doc, base, clash, self.index)
            self.views[self.index] = view.Id

        self.uidoc.ActiveView = self.doc.GetElement(self.views[self.index])

    def on_close(self, sender, event):
        self.Close()


def main():
    clashes, stats = detect_clashes(DOC)
    failures = stats.get("boolean_failures", 0)
    warn = " ({} paires ignorées: booléen impossible)".format(failures) if failures else ""

    if not clashes:
        return {
            "status": "no_clash",
            "message": "Aucun conflit trouvé entre les maquettes liées." + warn,
            "count": 0,
            "clashes": [],
            "context": CONTEXT,
            "stats": stats,
        }

    if uidoc is None:
        return {
            "status": "ok_no_ui",
            "message": "Conflits détectés, mais UI indisponible (session non interactive)." + warn,
            "count": len(clashes),
            "clashes": clashes,
            "context": CONTEXT,
            "stats": stats,
        }

    form = ClashBrowser(DOC, uidoc, clashes)
    # ShowDialog fonctionne mieux que Application.Run dans Dynamo/Revit déjà interactif.
    form.ShowDialog()
    return {
        "status": "ok",
        "message": "{} conflit(s) détecté(s).".format(len(clashes)) + warn,
        "count": len(clashes),
        "clashes": clashes,
        "context": CONTEXT,
        "stats": stats,
    }


# Exécuter automatiquement (pyRevit et Dynamo).
try:
    RESULT = main()
except Exception as ex:
    RESULT = {"status": "error", "message": str(ex), "count": 0, "clashes": [], "context": None}
    try:
        MessageBox.Show("Erreur script conflit Revit:\n{}".format(ex))
    except Exception:
        pass

OUT = RESULT
