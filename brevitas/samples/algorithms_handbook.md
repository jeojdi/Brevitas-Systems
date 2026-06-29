# A Field Guide to Algorithms

An original, self-contained primer written for the Brevitas demo. It is organized into clearly
separated sections so that a retrieval question ("explain quicksort") pulls only the relevant
slice, while a broad question ("summarize the whole guide") needs the full document. Nothing here
is copied from any textbook; it exists to exercise the demo.

## 1. Complexity and Big-O Notation

Big-O notation describes how the running time or memory of an algorithm grows as the input size
n grows, ignoring constant factors and lower-order terms. It captures the worst-case asymptotic
behavior. Common classes, from fastest to slowest: O(1) constant, O(log n) logarithmic,
O(n) linear, O(n log n) linearithmic, O(n^2) quadratic, O(2^n) exponential, and O(n!) factorial.

The point of Big-O is to compare algorithms independently of hardware. An O(n log n) sort will
eventually beat an O(n^2) sort for large enough n no matter how fast the slower machine is. We
also use Big-Omega for the best case and Big-Theta when the upper and lower bounds match.

A practical rule of thumb: nested loops over the same input usually signal O(n^2); halving the
problem each step signals O(log n); doing linear work after a halving signals O(n log n).

## 2. Searching

### Linear search
Linear search scans every element until it finds the target or exhausts the list. It runs in
O(n) time and works on unsorted data. It is the only option when the data has no order.

### Binary search
Binary search works only on a sorted array. It maintains a low and high boundary, inspects the
middle element, and discards half of the remaining range on each step. Because the search range
halves every iteration, it runs in O(log n) time. For a million sorted items, binary search needs
at most about twenty comparisons, while linear search may need a million. The classic pitfall is
computing the midpoint as (low + high) / 2, which can overflow; prefer low + (high - low) / 2.

## 3. Sorting

### Bubble sort
Bubble sort repeatedly steps through the list, swapping adjacent out-of-order elements. It is
O(n^2) and mainly of teaching interest, though it is adaptive: a nearly sorted list with an early
exit check can approach O(n).

### Merge sort
Merge sort is a divide-and-conquer algorithm. It splits the array in half, recursively sorts each
half, and merges the two sorted halves in linear time. It runs in O(n log n) in the worst case and
is stable, but it needs O(n) extra space for the merge. It is a strong default when stability and
predictable worst-case behavior matter.

### Quicksort
Quicksort also divides and conquers, but in place. It chooses a pivot, partitions the array so
that smaller elements precede the pivot and larger ones follow, then recursively sorts the two
partitions. The partition step is the heart of quicksort: it scans from both ends, swapping
elements that are on the wrong side of the pivot, until the pointers cross. Average time is
O(n log n) and it is usually faster than merge sort in practice due to good cache behavior, but a
poorly chosen pivot on already-sorted input degrades it to O(n^2). Randomizing the pivot or using
the median-of-three rule avoids that worst case in practice.

### Heapsort
Heapsort builds a binary heap and repeatedly extracts the maximum. It guarantees O(n log n) and
sorts in place, but it is not stable and tends to be slower than quicksort in practice.

## 4. Hash Tables

A hash table stores key-value pairs and offers average O(1) lookup, insertion, and deletion. A
hash function maps a key to a bucket index. Because many keys can map to the same bucket, hash
tables must handle collisions. Two main strategies exist. Separate chaining keeps a linked list
(or small tree) per bucket and appends collisions to it. Open addressing instead probes for the
next free slot using linear probing, quadratic probing, or double hashing. As the load factor —
the ratio of stored entries to buckets — rises, collisions increase and performance degrades, so
hash tables resize and rehash once the load factor crosses a threshold such as 0.75. A bad hash
function that clusters keys can push lookups toward O(n) in the worst case.

## 5. Graphs

A graph is a set of vertices connected by edges; edges may be directed or undirected and may
carry weights. Two standard representations are the adjacency list (space-efficient for sparse
graphs) and the adjacency matrix (fast edge lookups, O(V^2) space).

### Breadth-first search (BFS)
BFS explores a graph level by level using a queue. It visits all neighbors at the current depth
before moving deeper. On an unweighted graph, BFS finds the shortest path in terms of edge count.
It runs in O(V + E).

### Depth-first search (DFS)
DFS explores as far as possible along each branch before backtracking, using a stack or recursion.
It is the basis for cycle detection, topological sorting, and finding connected components. It
also runs in O(V + E). The key difference from BFS is the order of exploration: DFS dives deep,
BFS fans out wide.

### Dijkstra's algorithm
Dijkstra's algorithm finds the shortest path from a single source to all other vertices in a graph
with non-negative edge weights. It maintains a set of finalized distances and repeatedly selects
the unvisited vertex with the smallest tentative distance, relaxing its outgoing edges. With a
binary heap as the priority queue it runs in O((V + E) log V). It fails on graphs with negative
edges; for those use Bellman-Ford, which runs in O(V·E) and can detect negative cycles.

## 6. Dynamic Programming

Dynamic programming (DP) solves a problem by breaking it into overlapping subproblems and storing
each subproblem's answer so it is computed only once. It applies when a problem has optimal
substructure (an optimal solution is built from optimal solutions to subproblems) and overlapping
subproblems (the same subproblems recur). Two styles exist: top-down memoization, which caches
results of a recursion, and bottom-up tabulation, which fills a table iteratively. Classic
examples include Fibonacci numbers, the knapsack problem, longest common subsequence, and
edit distance. DP trades memory for time: it turns exponential brute-force recursion into
polynomial time by never recomputing a subproblem.

## 7. Greedy Algorithms

A greedy algorithm builds a solution step by step, always taking the choice that looks best at the
moment, never reconsidering. Greedy works only when a problem has the greedy-choice property and
optimal substructure. When it works it is simple and fast. Examples include Huffman coding for
compression, Kruskal's and Prim's algorithms for minimum spanning trees, and interval scheduling
where you repeatedly pick the activity that finishes earliest. The danger is that greedy can be
wrong: making change with arbitrary coin denominations, for instance, may need DP instead, because
the locally best coin choice can leave an unsolvable remainder.

## 8. Trees and Binary Search Trees

A tree is a connected acyclic graph with a designated root. A binary tree gives each node at most
two children. A binary search tree (BST) maintains the invariant that every key in a node's left
subtree is smaller than the node, and every key in the right subtree is larger. This ordering makes
search, insertion, and deletion run in O(h) time where h is the height. For a balanced tree h is
O(log n), but a BST built from sorted input degenerates into a linked list with h = n, pushing
operations to O(n). In-order traversal of a BST visits keys in sorted order, which is why trees are
preferred over hash tables when ordered iteration matters.

Tree traversals come in three depth-first orders — pre-order (node, left, right), in-order (left,
node, right), and post-order (left, right, node) — plus the breadth-first level-order traversal.
Pre-order is used to copy a tree, post-order to delete one, and in-order to read a BST in sorted
order.

## 9. Balanced Trees

Because plain BSTs can degenerate, self-balancing trees enforce a height bound through rotations.
A red-black tree colors nodes red or black and guarantees the longest root-to-leaf path is at most
twice the shortest, keeping height O(log n); it is the structure behind many standard-library
ordered maps. An AVL tree keeps the heights of a node's two subtrees within one of each other,
giving slightly stricter balance and faster lookups at the cost of more rotations on insert. A
B-tree generalizes the idea to many children per node and is the workhorse of databases and file
systems, because its high fan-out minimizes expensive disk reads.

## 10. Union-Find (Disjoint Set Union)

The union-find structure tracks a partition of elements into disjoint sets and supports two
operations: find, which returns the representative of an element's set, and union, which merges two
sets. With the two standard optimizations — union by rank and path compression — both operations run
in nearly constant amortized time, formally the inverse Ackermann function, which is at most four
for any practical input. Union-find is the engine behind Kruskal's minimum-spanning-tree algorithm
and is widely used for connectivity queries and cycle detection in undirected graphs.

## 11. Strings and Pattern Matching

Naive substring search compares the pattern against every position in the text, costing O(n·m) for
a text of length n and pattern of length m. The Knuth-Morris-Pratt (KMP) algorithm precomputes a
prefix table so that, on a mismatch, it shifts the pattern without re-examining characters, giving
O(n + m). The Rabin-Karp algorithm hashes the pattern and each text window using a rolling hash,
comparing hashes in constant time and the full strings only on a hash match; it is especially good
for multiple-pattern search. For prefix queries and autocomplete, a trie stores strings character by
character along paths from the root, giving O(L) lookup for a string of length L regardless of how
many strings are stored.

## 12. Recursion and Backtracking

Recursion solves a problem by reducing it to smaller instances of itself, with a base case to stop.
Every recursion has an implicit call stack, so deep recursion risks a stack overflow and can often
be converted to an explicit stack or an iterative loop. Backtracking is recursion that builds a
candidate solution incrementally and abandons a partial candidate ("backtracks") as soon as it
cannot possibly be completed. It systematically explores the solution space as a tree and prunes
branches that violate constraints. Classic backtracking problems include the N-queens puzzle,
Sudoku solving, generating permutations and combinations, and the maze/path-finding problems. The
art of backtracking is in pruning early: the sooner an invalid branch is detected, the smaller the
search tree.

## 13. Bit Manipulation

Many algorithms exploit the binary representation of integers for speed. Shifting left by one
multiplies by two; shifting right divides by two. The bitwise AND of n and n-1 clears the lowest set
bit, a trick used to count set bits quickly (Brian Kernighan's algorithm). XOR is its own inverse,
so XOR-ing all elements of an array where every value appears twice except one isolates the unique
value in O(n) time and O(1) space. Bitmasks compactly represent subsets, which is the basis of
bitmask dynamic programming over small sets such as the traveling-salesman DP.

## 14. Topological Sort

A topological sort orders the vertices of a directed acyclic graph (DAG) so that every edge points
from an earlier vertex to a later one. It answers questions like "in what order can I run these
tasks given their dependencies?" Two standard methods exist. Kahn's algorithm repeatedly removes a
vertex with in-degree zero, appends it to the order, and decrements the in-degrees of its
neighbors; if any vertices remain with non-zero in-degree, the graph has a cycle and no topological
order exists. The DFS method runs depth-first search and pushes each vertex onto a stack as it
finishes; the reversed stack is a valid order. Both run in O(V + E). Topological sort underlies
build systems, course-prerequisite scheduling, and spreadsheet recalculation.

## 15. Shortest Paths Beyond Dijkstra

Dijkstra handles non-negative weights, but several variants matter. The A* search algorithm speeds
up point-to-point shortest paths by adding a heuristic estimate of the remaining distance to the
priority, focusing the search toward the goal; with an admissible heuristic that never overestimates,
A* is optimal and typically explores far fewer nodes than Dijkstra. Bellman-Ford handles negative
edge weights and detects negative cycles in O(V·E) by relaxing all edges V-1 times. The
Floyd-Warshall algorithm computes shortest paths between all pairs of vertices in O(V^3) using
dynamic programming over intermediate vertices, which is practical for dense graphs of moderate size.

## 16. Minimum Spanning Trees

A minimum spanning tree (MST) connects all vertices of a weighted undirected graph with the least
total edge weight and no cycles. Kruskal's algorithm sorts edges by weight and adds each edge that
does not form a cycle, using union-find to test connectivity, running in O(E log E). Prim's algorithm
grows a single tree from a starting vertex, repeatedly adding the cheapest edge that connects a new
vertex, running in O((V + E) log V) with a heap. Both are greedy and both are correct because of the
cut property: the lightest edge crossing any partition of the vertices is always safe to include.

## 17. Two Pointers and Sliding Window

The two-pointer technique uses two indices that move through a sequence to avoid a nested loop. On a
sorted array it can find a pair summing to a target in O(n) by moving the left pointer up when the
sum is too small and the right pointer down when it is too large. The sliding-window technique
maintains a contiguous range over a sequence, expanding the right edge to include new elements and
contracting the left edge when a constraint is violated, which turns many O(n^2) substring and
subarray problems into O(n). Typical uses include the longest substring without repeating
characters and the maximum sum of any window of fixed size.

## 18. Amortized Analysis

Amortized analysis measures the average cost of an operation over a worst-case sequence, rather than
the worst single operation. A dynamic array that doubles its capacity when full occasionally pays
O(n) to copy elements, but because doublings are rare, the amortized cost of an append is O(1). Three
methods formalize this: the aggregate method divides the total cost of a sequence by its length; the
accounting method charges each operation a little extra to "save up" for expensive ones; and the
potential method tracks a stored-energy function that pays for costly operations. Amortized bounds
explain why hash tables, dynamic arrays, and splay trees are fast in practice despite occasional
expensive steps.

## 19. Segment Trees and Fenwick Trees

When a program must answer many range queries (such as the sum or minimum over a sub-array) while
also updating individual elements, a segment tree stores aggregate values over ranges in a balanced
binary tree, supporting both query and update in O(log n). A Fenwick tree, or binary indexed tree,
is a more compact structure that supports prefix-sum queries and point updates in O(log n) using
clever bit manipulation of indices. Both beat the naive choice between an O(n) recompute per query
and an O(n) update of precomputed prefix sums.

## 20. Intractability and NP-Completeness

Not every problem has a known efficient algorithm. The class P contains problems solvable in
polynomial time; the class NP contains problems whose proposed solutions can be verified in
polynomial time. A problem is NP-complete if it is in NP and every other NP problem reduces to it,
making it among the hardest in NP; examples include boolean satisfiability, the traveling salesman
decision problem, graph coloring, and subset sum. Whether P equals NP is the central open question
of computer science. In practice, when a problem is NP-hard, engineers stop seeking an exact
polynomial algorithm and instead use approximation algorithms with provable quality bounds,
heuristics such as simulated annealing or genetic algorithms, or exact exponential methods that are
fast enough on the input sizes that actually occur.

## 21. Choosing an Algorithm

There is rarely a single best algorithm; the right choice depends on the data and constraints. Use
binary search when data is sorted and you query it often. Prefer merge sort when you need stability
or a guaranteed bound, and quicksort when average speed and low memory matter. Reach for a hash
table when you need fast membership tests and order does not matter, and a balanced tree when you
need ordered traversal. Use BFS for shortest unweighted paths, Dijkstra for weighted ones, DP when
subproblems overlap, and greedy only after you have convinced yourself the greedy-choice property
holds. Use union-find for connectivity, a trie for prefix search, and backtracking when you must
explore a constrained combinatorial space. The recurring theme of this guide is trading memory for
time and matching the structure of the data to the structure of the algorithm.
