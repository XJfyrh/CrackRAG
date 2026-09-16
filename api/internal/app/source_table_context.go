package app

import (
	"fmt"
	"math"
	"strings"

	pb "crackrag/api/gen/crackrag/v1"
)

type sourceTablePreambleLine struct {
	Text string    `json:"text"`
	BBox []float64 `json:"bbox"`
}

type sourceTableContext struct {
	Version              string                    `json:"version"`
	Page                 uint32                    `json:"page"`
	TableIndex           int                       `json:"table_index"`
	TableBBox            []float64                 `json:"table_bbox"`
	TableText            string                    `json:"table_text"`
	TableTextSHA256      string                    `json:"table_text_sha256"`
	PreambleLines        []sourceTablePreambleLine `json:"preamble_lines"`
	PrecedingTableBottom float64                   `json:"preceding_table_bottom"`
}

const sourceGeometryTolerance = 0.01

func sourceFinite(value float64) bool { return !math.IsNaN(value) && !math.IsInf(value, 0) }

func sourceRectInPage(box []float64, width, height float64) bool {
	if len(box) != 4 || !sourceFinite(width) || !sourceFinite(height) || width <= 0 || height <= 0 {
		return false
	}
	for _, value := range box {
		if !sourceFinite(value) {
			return false
		}
	}
	return box[0] >= -sourceGeometryTolerance && box[1] >= -sourceGeometryTolerance &&
		box[2] <= width+sourceGeometryTolerance && box[3] <= height+sourceGeometryTolerance && box[0] < box[2] && box[1] < box[3]
}

// Only ingestion-owned, same-page geometry can associate outside declarations
// with a table. Older page_context/footnote_context are deliberately not a
// fallback: even a matching complete textual prefix can occur in two tables.
// Legacy regions that contain their own scope/unit remain supported by the
// caller without using any external preamble.
func sourceBoundTablePreamble(r *pb.Region, context map[string]any) (string, bool, bool) {
	value, present := context["table_context"]
	if !present {
		return "", false, false
	}
	invalid := func() (string, bool, bool) { return "", false, true }
	var table sourceTableContext
	if strictJSON(marshal(value), &table) != nil || table.Version != "same-page-table-context-v1" ||
		!strings.HasSuffix(r.ParserVersion, ":m1-region-chunks-v4") || r.Kind != "table" || table.Page != r.Page || r.Page == 0 ||
		table.TableIndex < 0 || table.TableIndex >= 512 || stringAt(context, "region_source") != fmt.Sprintf("table-%d", table.TableIndex) ||
		!sourceRectInPage(r.Bbox, r.PageWidth, r.PageHeight) || !sourceRectInPage(table.TableBBox, r.PageWidth, r.PageHeight) ||
		table.TableText == "" || len(table.TableText) > 24000 || hashBytes([]byte(table.TableText)) != table.TableTextSHA256 ||
		r.Text == "" || !strings.Contains(table.TableText, r.Text) || len(table.PreambleLines) > 64 ||
		!sourceFinite(table.PrecedingTableBottom) || table.PrecedingTableBottom < 0 || table.PrecedingTableBottom > table.TableBBox[1]+sourceGeometryTolerance {
		return invalid()
	}
	for i, value := range r.Bbox {
		if math.Abs(value-table.TableBBox[i]) > sourceGeometryTolerance {
			return invalid()
		}
	}
	text := []string{}
	bytes := 0
	previous := table.PrecedingTableBottom
	for _, line := range table.PreambleLines {
		if strings.TrimSpace(line.Text) == "" || strings.ContainsAny(line.Text, "\r\n") || len(line.Text) > 2000 ||
			!sourceRectInPage(line.BBox, r.PageWidth, r.PageHeight) || line.BBox[1] < previous-sourceGeometryTolerance ||
			line.BBox[1] < table.PrecedingTableBottom-sourceGeometryTolerance || line.BBox[3] > table.TableBBox[1]+sourceGeometryTolerance ||
			math.Min(line.BBox[2], table.TableBBox[2])-math.Max(line.BBox[0], table.TableBBox[0]) <= 0 {
			return invalid()
		}
		previous = line.BBox[1]
		bytes += len(line.Text)
		if bytes > 12000 {
			return invalid()
		}
		text = append(text, strings.TrimSpace(line.Text))
	}
	return strings.Join(text, "\n"), true, false
}
