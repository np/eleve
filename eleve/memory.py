from __future__ import division
import math
import logging
import pickle
import gzip

from eleve.storage import Storage

from nltk.corpus import stopwords
TERMINAL = set(stopwords.words('english') + ['^', '$'])
def is_terminal(token):
    return token in TERMINAL

def entropy(counts):
    """ Calculate entropy from an iterator containing
    count of occurence for each value.

    Each count must be greater or equal to 1. If it is
    not the case, it will be set to 1.

    >>> entropy([1,1])
    1.0
    >>> entropy([1])
    0.0
    >>> print('{:.4f}'.format(entropy([1,1,0,5,2])))
    1.6577
    """
    c, psum = 0, 0
    for i in counts:
        c += max(i, 0)
        psum += i * math.log(max(i, 1), 2)
    if c == 0:
        return 0
    return math.log(c, 2) - psum / c

def mean_stdev(values):
    """ Calculate mean and standard deviation from values of an iterator.

    >>> mean_stdev([1,3])
    (2.0, 1.0)
    >>> mean_stdev([2,2])
    (2.0, 0.0)
    """
    a, q, k = 0, 0, 0
    for v in values:
        k += 1
        old_a = a
        a += (v - a) / k
        q += (v - old_a)*(v - a)
    return (a, math.sqrt(q / k))

class Postings:
    def __init__(self, memory_node):
        self.node = memory_node

    def items(self):
        """ Iterator to all the postings of the leafs """
        for i in self.node.childs.values():
            yield from i.postings.items()

    def __len__(self):
        # in case we have a looot of documents (more that 10 000), we could use a hyperloglog algorithm
        s = set()
        for docid, _ in self.items():
            s.add(docid)
        return len(s)
    
class MemoryNode(object):
    """ Node used by :class:`MemoryStorage`
    """
    
    # to take a little less memory
    __slots__ = ['count', 'entropy', 'childs']

    def __init__(self, count=0):
        self.count = count
        self.entropy = None
        self.childs = {}

    @property
    def postings(self):
        return Postings(self)

class MemoryLeaf(object):
    __slots__ = ['count', 'postings']

    def __init__(self, count=0):
        self.count = count
        self.postings = {}

    @property
    def entropy(self):
        return None

    @property
    def childs(self):
        return {}

class MemoryStorage(Storage):
    """ In-memory tree (made to be simple, no specific optimizations)
    """

    def __init__(self, depth, path=None):
        """
        :param depth: Maximum length of stored ngrams
        :param path: Path to the database (not used)
        """
        self.depth = depth
        self.root = MemoryNode()

        # normalization params :
        # one for each level
        # on each level : mean, stdev
        self.normalization = [(0,0)] * depth

        self.dirty = False

    def clear(self):
        self.root = MemoryNode()
        self.dirty = True
        return self

    @classmethod
    def load(cls, path):
        depth, root, normalization = pickle.load(gzip.GzipFile(path, 'rb'))
        s = cls(depth)
        s.root = root
        s.normalization = normalization
        return s

    def save(self, path):
        self.update_stats()
        o = (self.depth, self.root, self.normalization)
        with gzip.GzipFile(path, 'wb') as f:
            pickle.dump(o, f)

    def iter_leafs(self):
        def _rec(ngram, node):
            if node.childs:
                for k, c in node.childs.items():
                    for i in _rec(ngram + [k], c): yield i
            elif node is not self.root:
                yield ngram

        for i in _rec([], self.root): yield i

    def __iter__(self):
        """ Iterator on all the ngrams in the trie.
        Including partial ngrams (not leafs). So it gives a ngram for every node.
        """
        def _rec(ngram, node):
            yield (ngram, node.count)
            for k, c in node.childs.items():
                for i in _rec(ngram + [k], c): yield i

        for i in _rec([], self.root): yield i

    def update_stats(self):
        """ Update the internal statistics (like entropy, and stdev & means
        for the entropy variations. """
        if not self.dirty:
            return

        def update_entropy(node):
            counts = []
            for k, n in node.childs.items():
                if is_terminal(k):
                    counts.extend(1 for _ in range(n.count))
                else:
                    counts.append(n.count)
            node.entropy = entropy(counts)

            for child in node.childs.values():
                if isinstance(child, MemoryNode):
                    update_entropy(child)

        update_entropy(self.root)

        def ve_for_depth(node, parent, depth):
            if depth == 0:
                if node.entropy is not None and (node.entropy != 0 or parent.entropy != 0):
                    yield node.entropy - parent.entropy
            else:
                for child in node.childs.values():
                    for i in ve_for_depth(child, node, depth - 1): yield i

        for i in range(self.depth):
            try:
                self.normalization[i] = mean_stdev(ve_for_depth(self.root, None, i + 1))
            except ZeroDivisionError:
                pass

        self.dirty = False

    def _check_dirty(self):
        if self.dirty:
            logging.warning("Updating the tree statistics (update_stats method), as we query it while dirty. This is a slow operation.")
            self.update_stats()

    def add_ngram(self, ngram, docid, freq=1):
        """ Add a ngram to the tree.
        You can specify the number of times you add (or substract) that ngram by using the `freq` argument.
        """

        if not 0 < len(ngram) <= self.depth:
            raise ValueError("The size of the ngram parameter must be in range(1, {} + 1)".format(self.depth))

        self.dirty = True

        node = self.root
        node.count += freq

        for i, token in enumerate(ngram):
            try:
                node = node.childs[token]
                node.count += freq
            except KeyError:
                child = MemoryLeaf(freq) if i == len(ngram) - 1 else MemoryNode(freq)
                assert isinstance(node, MemoryNode)
                node.childs[token] = child
                node = child

        assert isinstance(node, MemoryLeaf)
        try:
            node.postings[docid] += freq
        except KeyError:
            node.postings[docid] = freq

    def _lookup(self, ngram):
        """ Search for a node and raises KeyError if the node doesn't exists """
        node = self.root
        last_node = node
        while ngram:
            last_node = node
            node = node.childs[ngram[0]]
            ngram = ngram[1:]
        return (last_node, node)

    def query_node(self, ngram):
        """ Return a tuple with the main node data : (count, entropy).
        Count is the number of ngrams starting with the ``ngram`` parameter, entropy the entropy after the ngram.
        """
        self._check_dirty()
        try:
            _, node = self._lookup(ngram)
        except KeyError:
            return (0, None)
        return (node.count, node.entropy)
    
    def query_postings(self, ngram):
        try:
            _, node = self._lookup(ngram)
        except KeyError:
            return {}
        return node.postings

    def query_ev(self, ngram):
        """ Return the entropy variation for the ngram.
        """
        self._check_dirty()

        if not ngram:
            return None

        try:
            last_node, node = self._lookup(ngram)
        except KeyError:
            return None
        if node.entropy is not None and (node.entropy != 0 or last_node.entropy != 0):
            return node.entropy - last_node.entropy
        return None

    def query_autonomy(self, ngram, z_score=True):
        """ Return the autonomy (normalized entropy variation) for the ngram.
        """
        if not ngram:
            raise ValueError("Can't query the autonomy of the root node.")
        self._check_dirty()
        mean, stdev = self.normalization[len(ngram) - 1]
        ev = self.query_ev(ngram)
        if ev is None:
            return None
        nev = ev - mean
        if z_score:
            nev /= stdev
        return nev

if __name__ == '__main__':
    import doctest
    doctest.testmod()
