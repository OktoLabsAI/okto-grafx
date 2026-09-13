"""Generate an offline example without touching any existing graph or output file."""
import argparse
from okto_grafx import connect
from okto_grafx.projections import project_graph
from okto_grafx.html_snapshot import render_html_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', help='New HTML output path (must not already exist)')
    args = parser.parse_args()
    with connect(':memory:') as db:
        with db.begin() as tx:
            tx.execute('CREATE NODE TABLE Task(id INT64, PRIMARY KEY(id))')
            tx.execute('CREATE REL TABLE DependsOn(FROM Task TO Task)')
            for identity in range(5):
                tx.execute('CREATE (:Task {id:$id})', {'id': identity})
            for a, b in [(0, 1), (1, 2), (2, 3), (3, 4), (0, 0), (0, 1)]:
                tx.execute('MATCH (a:Task {id:$a}),(b:Task {id:$b}) CREATE (a)-[:DependsOn]->(b)',
                           {'a': a, 'b': b})
        graph = project_graph(db, node_tables=('Task',), relationship_tables=('DependsOn',))
        catalog = db.catalog.catalog
    html = render_html_snapshot(graph, catalog=catalog)
    with open(args.output, 'x', encoding='utf-8') as output:
        output.write(html)


if __name__ == '__main__':
    main()
