from coffee import base as ast
from coffee.visitor import Visitor
from collections import OrderedDict

from ufl.algorithms.multifunction import MultiFunction

from gem import (Literal, Sum, Product, Indexed, ComponentTensor, IndexSum,
                 FlexiblyIndexed, Solve, Inverse, decompose_variable_view)  # , reshape)


from functools import singledispatch, update_wrapper
import firedrake
import firedrake.slate.slate as sl
import loopy as lp
from loopy.program import make_program
from loopy.transform.callable import inline_callable_kernel, register_callable_kernel
from loopy.kernel.creation import add_sequential_dependencies
from islpy import BasicSet
from gem.node import post_traversal

import numpy as np
import islpy as isl
import pymbolic.primitives as pym
import itertools


class RemoveRestrictions(MultiFunction):
    """UFL MultiFunction for removing any restrictions on the
    integrals of forms.
    """
    expr = MultiFunction.reuse_if_untouched

    def positive_restricted(self, o):
        return self(o.ufl_operands[0])


class SymbolWithFuncallIndexing(ast.Symbol):
    """A functionally equivalent representation of a `coffee.Symbol`,
    with modified output for rank calls. This is syntactically necessary
    when referring to symbols of Eigen::MatrixBase objects.
    """

    def _genpoints(self):
        """Parenthesize indices during loop assignment"""
        pt = lambda p: "%s" % p
        pt_ofs = lambda p, o: "%s*%s+%s" % (p, o[0], o[1])
        pt_ofs_stride = lambda p, o: "%s+%s" % (p, o)
        result = []

        if not self.offset:
            for p in self.rank:
                result.append(pt(p))
        else:
            for p, ofs in zip(self.rank, self.offset):
                if ofs == (1, 0):
                    result.append(pt(p))
                elif ofs[0] == 1:
                    result.append(pt_ofs_stride(p, ofs[1]))
                else:
                    result.append(pt_ofs(p, ofs))
        result = ', '.join(i for i in result)

        return "(%s)" % result


class Transformer(Visitor):
    """Replaces all out-put tensor references with a specified
    name of :type: `Eigen::Matrix` with appropriate shape. This
    class is primarily for COFFEE acrobatics, jumping through
    nodes and redefining where appropriate.

    The default name of :data:`"A"` is assigned, otherwise a
    specified name may be passed as the :data:`name` keyword
    argument when calling the visitor.
    """

    def visit_object(self, o, *args, **kwargs):
        """Visits an object and returns it.

        e.g. string ---> string
        """
        return o

    def visit_list(self, o, *args, **kwargs):
        """Visits an input of COFFEE objects and returns
        the complete list of said objects.
        """
        newlist = [self.visit(e, *args, **kwargs) for e in o]
        if all(newo is e for newo, e in zip(newlist, o)):
            return o

        return newlist

    visit_Node = Visitor.maybe_reconstruct

    def visit_FunDecl(self, o, *args, **kwargs):
        """Visits a COFFEE FunDecl object and reconstructs
        the FunDecl body and header to generate
        ``Eigen::MatrixBase`` C++ template functions.

        Creates a template function for each subkernel form.

        .. code-block:: c++

            template <typename Derived>
            static inline void foo(Eigen::MatrixBase<Derived> const & A, ...)
            {
              [Body...]
            }
        """
        name = kwargs.get("name", "A")
        new = self.visit_Node(o, *args, **kwargs)
        ops, okwargs = new.operands()
        if all(new is old for new, old in zip(ops, o.operands()[0])):
            return o

        ret, kernel_name, kernel_args, body, pred, headers, template = ops

        body_statements, _ = body.operands()
        decl_init = "const_cast<Eigen::MatrixBase<Derived> &>(%s_);\n" % name
        new_dec = ast.Decl(typ="Eigen::MatrixBase<Derived> &", sym=name,
                           init=decl_init)
        new_body = [new_dec] + body_statements
        eigen_template = "template <typename Derived>"

        new_ops = (ret, kernel_name, kernel_args,
                   new_body, pred, headers, eigen_template)

        return o.reconstruct(*new_ops, **okwargs)

    def visit_Decl(self, o, *args, **kwargs):
        """Visits a declared tensor and changes its type to
        :template: result `Eigen::MatrixBase<Derived>`.

        i.e. double A[n][m] ---> const Eigen::MatrixBase<Derived> &A_
        """
        name = kwargs.get("name", "A")
        if o.sym.symbol != name:
            return o
        newtype = "const Eigen::MatrixBase<Derived> &"

        return o.reconstruct(newtype, ast.Symbol("%s_" % name))

    def visit_Symbol(self, o, *args, **kwargs):
        """Visits a COFFEE symbol and redefines it as a Symbol with
        FunCall indexing.

        i.e. A[j][k] ---> A(j, k)
        """
        name = kwargs.get("name", "A")
        if o.symbol != name:
            return o

        return SymbolWithFuncallIndexing(o.symbol, o.rank, o.offset)


# singledispatch for class functions
def classsingledispatch(func):
    dispatcher = singledispatch(func)

    def wrapper(*args, **kw):
        return dispatcher.dispatch(args[1].__class__)(*args, **kw)

    wrapper.register = dispatcher.register
    update_wrapper(wrapper, func)
    return wrapper


class SlateTranslator():
    """Multifunction for translating UFL -> GEM.  """

    def __init__(self, builder):
        self.builder = builder
        self.decomp_dict = OrderedDict()

    def slate_to_gem_translate(self):
        translated_nodes = OrderedDict()
        traversed_dag = list(post_traversal([self.builder.expression]))

        # TODO I dont think we need this loop
        # First traversal for resolving tensors and assembled vectors
        for tensor in traversed_dag:  # tensor hier is actually TensorBase
            if isinstance(tensor, sl.Tensor) or isinstance(tensor, sl.AssembledVector):
                translated_nodes.setdefault(tensor, self.slate_to_gem(tensor, translated_nodes))

        # Second traversal for other nodes
        for tensor in traversed_dag[:len(traversed_dag)-1]:
            # other tensor types are translated into gem nodes
            if not isinstance(tensor, sl.Tensor) and not isinstance(tensor, sl.AssembledVector):
                translated_nodes.setdefault(tensor, self.slate_to_gem(tensor, translated_nodes))

        # Last root contains the whole tree
        tree = self.slate_to_gem(traversed_dag[len(traversed_dag)-1], translated_nodes)
        return tree

    @classsingledispatch
    def slate_to_gem(self, tensor, node_dict):
        """Translates slate tensors into GEM.
        :returns: GEM translation of the modified terminal
        """
        raise AssertionError("Cannot handle terminal type: %s" % type(tensor))

    @slate_to_gem.register(firedrake.slate.slate.Tensor)
    def slate_to_gem_tensor(self, tensor, node_dict):
        return self.builder.temps[tensor]

    @slate_to_gem.register(firedrake.slate.slate.AssembledVector)
    def slate_to_gem_vector(self, tensor, node_dict):
        ret = None
        # Not mixed tensor can just be translated into the right gem Node saved in builder
        if len(tensor.shapes) == 1 and not tensor.is_mixed:
            coeffs = self.builder.coefficient_vecs[tensor.shapes[0][0]]
            for coeff in coeffs:
                if coeff.vector == tensor:
                    assert ret is None, "This vector as already been assembled."
                    ret = coeff.local_temp
        # Mixed assembled vectors need to be translated into FlexiblyIndexed
        # This is similar to blocks
        else:
            dim2idxs = []
            for dofs, cinfo_list in self.builder.coefficient_vecs.items():
                for i, cinfo in enumerate(cinfo_list):
                    if cinfo.vector == tensor:
                        var = cinfo.local_temp.children[0]
                        self.builder.create_index(cinfo.shape, str(cinfo)+"mixed")
                        index = self.builder.gem_indices[str(cinfo)+"mixed"]
                        dim2idxs.append(tuple([cinfo.offset_index, ((index[0], 1), )]))
            ret = FlexiblyIndexed(var, dim2idxs)
        return ret

    @slate_to_gem.register(firedrake.slate.slate.Add)
    def slate_to_gem_add(self, tensor, node_dict):
        A, B = tensor.operands  # slate tensors
        _A, _B = node_dict[A], node_dict[B]  # gem representations
        self.builder.create_index(A.shape, str(A)+"newadd"+str(B))
        new_indices = self.builder.gem_indices[str(A)+"newadd"+str(B)]
        self.builder.create_index(tensor.shape, tensor)
        out_indices = self.builder.gem_indices[tensor]
        _A = self.get_tensor_withnewidx(_A, new_indices)
        _B = self.get_tensor_withnewidx(_B, new_indices)
        return Indexed(ComponentTensor(Sum(_A, _B), new_indices), out_indices)

    @slate_to_gem.register(firedrake.slate.slate.Negative)
    def slate_to_gem_negative(self, tensor, node_dict):
        A, = tensor.operands
        self.builder.create_index(A.shape, str(A)+"newneg")
        new_indices = self.builder.gem_indices[str(A)+"newneg"]
        self.builder.create_index(tensor.shape, tensor)
        out_indices = self.builder.gem_indices[tensor]
        var_A = self.get_tensor_withnewidx(node_dict[A], new_indices)
        return Indexed(ComponentTensor(Product(Literal(-1), var_A), new_indices), out_indices)

    @slate_to_gem.register(firedrake.slate.slate.Transpose)
    def slate_to_gem_transpose(self, tensor, node_dict):
        A, = tensor.operands
        _A = node_dict[A]
        self.builder.create_index(A.shape, str(A)+"newtrans")
        new_indices = self.builder.gem_indices[str(A)+"newtrans"]
        self.builder.create_index(tensor.shape, tensor)
        out_indices = self.builder.gem_indices[tensor]
        var_A = self.get_tensor_withnewidx(_A, new_indices)
        ret = Indexed(ComponentTensor(var_A, new_indices[::-1]), out_indices)
        return ret

    @slate_to_gem.register(firedrake.slate.slate.Mul)
    def slate_to_gem_mul(self, tensor, node_dict):
        A, B = tensor.operands
        var_A, var_B = node_dict[A], node_dict[B]  # gem representations

        # New indices are necessary in case as Tensor gets multiplied with itself.
        self.builder.create_index(A.shape, str(A)+"newmulA"+str(tensor))
        new_indices_A = self.builder.gem_indices[str(A)+"newmulA"+str(tensor)]
        self.builder.create_index(B.shape, str(B)+"newmulB"+str(tensor))
        new_indices_B = self.builder.gem_indices[str(B)+"newmulB"+str(tensor)]

        self.builder.create_index(tensor.shape, str(tensor)+str(A)+str(B))
        out_indices = self.builder.gem_indices[str(tensor)+str(A)+str(B)]

        if len(A.shape) == len(B.shape) and A.shape[1] == B.shape[0]:
            var_A = self.get_tensor_withnewidx(var_A, new_indices_A)
            var_B = self.get_tensor_withnewidx(var_B, (new_indices_A[1], new_indices_B[1]))

            prod = Product(var_A, var_B)
            sum = IndexSum(prod, (new_indices_A[1],))
            return self.get_tensor_withnewidx(sum, out_indices)

        elif len(A.shape) > len(B.shape) and A.shape[1] == B.shape[0]:
            var_A = self.get_tensor_withnewidx(var_A, new_indices_A)
            var_B = self.get_tensor_withnewidx(var_B, (new_indices_A[1],))

            prod = Product(var_A, var_B)
            sum = IndexSum(prod, (new_indices_A[1],))
            return self.get_tensor_withnewidx(sum, out_indices)
        else:
            return[]

    # TODO Try to translate the slate inverse to gem FlexiblyIndexed
    # @slate_to_gem.register(firedrake.slate.slate.Mul)
    # def slate_to_gem_mul(self, tensor, node_dict):
    #     A, B = tensor.operands
    #     var_A, var_B = node_dict[A], node_dict[B]  # gem representations

    #     # New indices are necessary in case as Tensor gets multiplied with itself.
    #     self.builder.create_index(A.shapes, str(A)+"newmulA"+str(tensor))
    #     new_indices_A = self.builder.gem_indices[str(A)+"newmulA"+str(tensor)]
    #     self.builder.create_index(B.shapes, str(B)+"newmulB"+str(tensor))
    #     new_indices_B = self.builder.gem_indices[str(B)+"newmulB"+str(tensor)]

    #     self.builder.create_index(tensor.shapes, str(tensor)+str(A)+str(B))
    #     out_indices = self.builder.gem_indices[str(tensor)+str(A)+str(B)]

    #     if len(A.shape) == len(B.shape) and A.shape[1] == B.shape[0]:
    #         var_A = self.get_tensor_withnewidx(var_A, new_indices_A)
    #         var_B = self.get_tensor_withnewidx(var_B, (new_indices_A[1], new_indices_B[1]))

    #         prod = Product(var_A, var_B)
    #         sum = IndexSum(prod, new_indices_A[1])
    #         return self.get_tensor_withnewidx(sum, out_indices)

    #     elif len(A.shape) > len(B.shape) and A.shape[1] == B.shape[0]:
    #         var_A = self.get_tensor_withnewidx(var_A, new_indices_A)
    #         var_B = self.get_tensor_withnewidx(var_B, (new_indices_A[1],))

    #         prod = Product(var_A, var_B)
    #         sum = IndexSum(prod, (new_indices_A[1],))
    #         return self.get_tensor_withnewidx(sum, out_indices)
    #     else:
    #         return[]

    @slate_to_gem.register(firedrake.slate.slate.Block)
    def slate_to_gem_blocks(self, tensor, node_dict):

        A, = tensor.operands
        node = node_dict[A]
        dim2idxs = [[], []]

        # rangification
        indices = ()
        for index in tensor._indices:
            if type(index) == int:
                indices += (range(index, index+1), )
            else:
                indices += (index, )
        loop = itertools.product(*indices)

        # also treats a range in blocks
        # A = [00, 01, 02; 10, 11, 12; 20, 21, 22]
        # ret = A[2:3, 2:3][0,1] -> A[2,3]
        for c, block in enumerate(loop):
            # i points to the block matrices
            # idx points to the shape of that block matrix in all dimensions
            Aoffset = ()
            extent = ()
            for i, idx in enumerate(block):

                # move the original index by an offset
                # to reference into the subblock of the tensor which
                # dim2idxs is of form ((offset, ((index, stride), ) ... ), (offset, ((index, stride), ) ...))
                # ((1, ((i, 12), (j, 4), (k, 1))), (0, ())) ->  variable[1 + i*12 + j*4 + k][0]
                # dim2idx[dim][0]-> offset
                # dim2idx[dim][1]-> (index, stride)
                if c == 0:
                    if isinstance(idx, int):
                        Aoffset += (sum(A.shapes[i][:idx]), )
                        extent += (A.shapes[i][idx], )
                    elif isinstance(idx, range):
                        if idx.start == 0:
                            Aoffset += (0, )
                        else:
                            Aoffset += ((A.shapes[i][idx.start-1]), )
                        extent += (sum(A.shapes[i][:idx.stop]), )

                elif len(dim2idxs[i][1]) < (indices[i].stop - indices[i].start):
                    extent += (A.shapes[i][idx], )

            # TODO I think this only works when the blocks have the same size
            # otherwise block needs to produce multiple indices
            # and in block(block) the right index needs to be picked up from the inside
            if c == 0:
                key = tensor
                self.builder.create_index(extent, key)
                gem_index = self.builder.gem_indices[key]

                for i, idx in enumerate(gem_index):
                    idxs = ((gem_index[i], 1), )
                    dim2idxs[i].extend([Aoffset[i], idxs])
            else:
                key = str(tensor) + str(block) + str(i)
                self.builder.create_index(extent, key)
                gem_index = self.builder.gem_indices[key]

                for i, idx in enumerate(gem_index):
                    if len(dim2idxs[i][1]) < (indices[i].stop - indices[i].start) \
                            and extent[i] == tensor.shapes[0][len(dim2idxs[i][1])]:
                        idxs = ((gem_index[i], 1), )
                        dim2idxs[i][1] += idxs
                    elif len(gem_index) == 1 and len(dim2idxs[1][1]) < (indices[1].stop - indices[1].start):
                        idxs = ((gem_index[0], 1), )
                        dim2idxs[1][1] += idxs

        if type(node) == FlexiblyIndexed:
            for i, idx in enumerate(gem_index):
                dim2idxs[i][0] += node_dict[A].dim2idxs[i][0]
                dim2idxs[i] = tuple(dim2idxs[i])
            ret = FlexiblyIndexed(node_dict[A].children[0], tuple(dim2idxs))
        else:
            for i, idx in enumerate(gem_index):
                dim2idxs[i] = tuple(dim2idxs[i])
            ret = FlexiblyIndexed(node_dict[A].children[0], tuple(dim2idxs))
        return ret

    def get_tensor_withnewidx(self, var, idx):
        """ This method is a helper to:
        - pick up free indices from a variable in the right
        order
        - if needed (it is for IndexSum and Indexed),
        pull up a scalar variable to a tensor thing with this indices
        - index it with new indices :arg idx.
        """
        if type(var) == Indexed:
            # No unnecessary generation of Indexed(ComponentTensor)
            if var.free_indices == idx:
                var = Indexed(var.children[0], idx)
            else:
                # Sort free indices by the multiindex
                free_indices_sorted = tuple()
                for index in var.multiindex:
                    if index in var.free_indices:
                        free_indices_sorted += (index, )
                # Special case for IndexSum generating an Indexed with scalar multiindex
                # e.g. occurs for DG0
                # picks up free indices with are not in multiindex
                for index in var.free_indices:
                    if index not in free_indices_sorted:
                        free_indices_sorted += (index, )
                # return Indexed with new indices
                var = Indexed(ComponentTensor(var, free_indices_sorted), idx)

        elif type(var) == IndexSum:
            # TODO this might not be robust
            # Sort free indices
            free_indices_sorted = tuple()
            for i, index in enumerate(var.free_indices):
                if type(var.children[0].children[i]) == FlexiblyIndexed:
                    ordered_indexed = var.children[0].children[i].dim2idxs[i][1][0][0]
                else:
                    ordered_indexed = var.children[0].children[i].multiindex[i]
                if index not in free_indices_sorted and index == ordered_indexed:
                    free_indices_sorted += (index, )
            # Sometimes free indices are just reversed
            if free_indices_sorted == ():
                free_indices_sorted = var.free_indices[::-1]
            # return Indexed with new indices
            var = Indexed(ComponentTensor(var, free_indices_sorted), idx)

        elif type(var) == FlexiblyIndexed:
            variable, dim2idxs, [] = decompose_variable_view(ComponentTensor(var, var.free_indices))
            dim2idxs_new = ()
            for i, dim in enumerate(dim2idxs):
                if isinstance(idx[i], tuple):  # variable contains something mixed
                    index = tuple((idx_per_dim, 1) for idx_per_dim in idx[i])
                    dim2idxs_new += ((dim2idxs[i][0], index),)
                else:  # variable contains something not mixed
                    dim2idxs_new += ((dim2idxs[i][0], idx),)
            var = FlexiblyIndexed(variable, dim2idxs_new)
        else:
            assert "Variable type is "+str(type(var))+". Must be a scalar type."
        return var

    # TODO change to suit flexiblyindexed
    # def get_tensor_withnewidx(self, var, idx):
    #     if type(var) == Indexed:
    #         if var.free_indices == idx:
    #             # no unnecessary generation of Indexed(ComponentTensor)
    #             var = Indexed(var.children[0], idx)
    #         else:
    #             free_indices_sorted = tuple()
    #             for index in var.multiindex:
    #                 if index in var.free_indices:
    #                     free_indices_sorted += (index, )
    #             # special case for IndexSum generating an Indexed with scalar multiindex
    #             # # e.g. occurs for DG0
    #             for index in var.free_indices:
    #                 if index not in free_indices_sorted:
    #                     free_indices_sorted += (index, )
    #             var = Indexed(ComponentTensor(var, free_indices_sorted), idx)
    #     elif type(var) == IndexSum:
    #         free_indices_sorted = tuple()
    #         # for i, index in enumerate(var.free_indices):
    #         #     if type(var.children[0].children[i]) == FlexiblyIndexed:
    #         #         ordered_indexed = var.children[0].children[i].dim2idxs[i][1][0][0]
    #         #     else:
    #         #         ordered_indexed = var.children[0].children[i].multiindex[i]
    #         #     if index not in free_indices_sorted and index == ordered_indexed:
    #         #         free_indices_sorted += (index, )
    #         if free_indices_sorted == ():
    #             free_indices_sorted = var.free_indices[::-1]
    #         var = Indexed(ComponentTensor(var, free_indices_sorted), idx)
    #     elif type(var) == FlexiblyIndexed:
    #         variable, dim2idxs, indexes = decompose_variable_view(ComponentTensor(var, var.free_indices))
    #         dim2idxs_new = ()
    #         for i, dim in enumerate(dim2idxs):
    #             # dim2idxs_new += ((dim2idxs[i][0], ((idx[i], 1),)),)
    #             if isinstance(idx[i],tuple):
    #                 index = tuple((idx_per_dim, 1) for idx_per_dim in idx[i])
    #                 dim2idxs_new += ((dim2idxs[i][0], index),)
    #             else:
    #                 dim2idxs_new += ((dim2idxs[i][0], idx),)
    #         var = FlexiblyIndexed(variable, dim2idxs_new)
    #     else:
    #         assert "Variable type is "+str(type(var))+". Must be Indexed."
    #     return var

    @slate_to_gem.register(firedrake.slate.slate.Solve)
    def slate_to_gem_solve(self, tensor, node_dict):
        fac, B = tensor.operands  # TODO is first operand always factorization?
        A, = fac.operands
        self.builder.create_index(A.shape, str(tensor)+"readssolve")
        A_indices = self.builder.gem_indices[str(tensor)+"readssolve"]
        self.builder.create_index(B.shape, str(tensor)+"readsbsolve")
        B_indices = self.builder.gem_indices[str(tensor)+"readsbsolve"]
        self.builder.create_index(tensor.shape, tensor)
        new_indices = self.builder.gem_indices[tensor]
        ret_A = ComponentTensor(self.get_tensor_withnewidx(node_dict[A], A_indices), A_indices)
        ret_B = ComponentTensor(self.get_tensor_withnewidx(node_dict[B], B_indices), B_indices)
        ret = Indexed(Solve(ret_A, ret_B, new_indices), new_indices)
        return ret

    @slate_to_gem.register(firedrake.slate.slate.Inverse)
    def slate_to_gem_inverse(self, tensor, node_dict):
        A, = tensor.operands
        self.builder.create_index(A.shape, str(A)+"readsinv")
        A_indices = self.builder.gem_indices[str(A)+"readsinv"]
        self.builder.create_index(tensor.shape, tensor)
        new_indices = self.builder.gem_indices[tensor]
        self.builder.create_index(tensor.shape, str(tensor)+"outinv")
        out_indices = self.builder.gem_indices[str(tensor)+"outinv"]
        ret = ComponentTensor(self.get_tensor_withnewidx(node_dict[A], A_indices), A_indices)
        ret = Indexed(Inverse(ret, new_indices), out_indices)
        return ret

    # TODO Try to translate the slate inverse to gem FlexiblyIndexed
    # @slate_to_gem.register(firedrake.slate.slate.Inverse)
    # def slate_to_gem_inverse(self, tensor, node_dict):
    #     A, = tensor.operands
    #     self.builder.create_index(A.shapes, str(A)+"readsinv")
    #     A_indices = self.builder.gem_indices[str(A)+"readsinv"]
    #     A_multiindices = tuple(i for idx in A_indices for i in idx )
    #     new = self.get_tensor_withnewidx(node_dict[A], A_indices)
    #     self.builder.create_index(tensor.shapes, str(tensor)+"outinv")
    #     idx = self.builder.gem_indices[str(tensor)+"outinv"]
    #     inverseof = ComponentTensor(new, A_multiindices)
    #     [], dim2idxs, [] = decompose_variable_view(inverseof)
    #     dim2idxs_new = ()
    #     for i, dim in enumerate(dim2idxs):
    #         if isinstance(idx[i],tuple):
    #             index = tuple((idx_per_dim, 1) for idx_per_dim in idx[i])
    #             dim2idxs_new += ((dim2idxs[i][0], index),)
    #     ret = Inverse(inverseof, multiindices)
    #     ret = FlexiblyIndexed(ret, dim2idxs_new)
    #     return ret

    # TODO how do we deal with surpressed factorization nodes,
    # maybe populate decompdict and pass through to loopy later?
    @slate_to_gem.register(firedrake.slate.slate.Factorization)
    def slate_to_gem_factorization(self, tensor, node_dict):
        self.decomp_dict.setdefault(tensor, tensor.decomposition)
        return []


def eigen_tensor(expr, temporary, index):
    """Returns an appropriate assignment statement for populating a particular
    `Eigen::MatrixBase` tensor. If the tensor is mixed, then access to the
    :meth:`block` of the eigen tensor is provided. Otherwise, no block
    information is needed and the tensor is returned as is.

    :arg expr: a `slate.Tensor` node.
    :arg temporary: the associated temporary of the expr argument.
    :arg index: a tuple of integers used to determine row and column
                information. This is provided by the SplitKernel
                associated with the expr.
    """
    if expr.is_mixed:
        try:
            row, col = index
        except ValueError:
            row = index[0]
            col = 0
        rshape = expr.shapes[0][row]
        rstart = sum(expr.shapes[0][:row])
        try:
            cshape = expr.shapes[1][col]
            cstart = sum(expr.shapes[1][:col])
        except KeyError:
            cshape = 1
            cstart = 0

        tensor = ast.FlatBlock("%s.block<%d, %d>(%d, %d)" % (temporary,
                                                             rshape, cshape,
                                                             rstart, cstart))
    else:
        tensor = temporary

    return tensor


def depth_first_search(graph, node, visited, schedule):
    """A recursive depth-first search (DFS) algorithm for
    traversing a DAG consisting of Slate expressions.

    :arg graph: A DAG whose nodes (vertices) are Slate expressions
                with edges connected to dependent expressions.
    :arg node: A starting vertex.
    :arg visited: A set keeping track of visited nodes.
    :arg schedule: A list of reverse-postordered nodes. This list is
                   used to produce a topologically sorted list of
                   Slate nodes.
    """
    if node not in visited:
        visited.add(node)

        for n in graph[node]:
            depth_first_search(graph, n, visited, schedule)

        schedule.append(node)


def topological_sort(exprs):
    """Topologically sorts a list of Slate expressions. The
    expression graph is constructed by relating each Slate
    node with a list of dependent Slate nodes.

    :arg exprs: A list of Slate expressions.
    """
    graph = OrderedDict((expr, set(traverse_dags([expr])) - {expr})
                        for expr in exprs)

    schedule = []
    visited = set()
    for n in graph:
        depth_first_search(graph, n, visited, schedule)

    return schedule


def traverse_dags(exprs):
    """Traverses a set of DAGs and returns each node.

    :arg exprs: An iterable of Slate expressions.
    """
    seen = set()
    container = []
    for tensor in exprs:
        if tensor not in seen:
            seen.add(tensor)
            container.append(tensor)
    while container:
        tensor = container.pop()
        yield tensor

        for operand in tensor.operands:
            if operand not in seen:
                seen.add(operand)
                container.append(operand)


def merge_loopy(loopy_outer, loopy_inner_list, builder):
    # Generate initilisation instructions for all tensor temporaries
    inits = []
    c = 0
    for slate_tensor, gem_indexed in builder.temps.items():
        # Create new indices for inits and save with indexed (gem) key instead of slate tensor
        shape = builder.shape(slate_tensor)
        indices = builder.create_index(shape, gem_indexed)
        loopy_tensor = builder.gem_loopy_dict[gem_indexed]
        indices = builder.loopy_indices[gem_indexed]
        inames = {var.name for var in indices}
        inits.append(lp.Assignment(pym.Subscript(pym.Variable(loopy_tensor.name), indices), 0.0, id="init%d" % c, within_inames=frozenset(inames)))
        c += 1

    # Generate initilisation instructions for all coefficent temporaries,
    # which are in an AssembledVector
    # Same difficulty as adding those as global args (see comment in compiler)
    coeff_shape_list = []
    coeff_function_list = []
    coeff_tensor_list = []
    coeff_offset_list = []
    for v in builder.coefficient_vecs.values():
        for coeff_info in v:
            coeff_shape_list.append(coeff_info.shape)
            coeff_function_list.append(coeff_info.vector._function)
            coeff_tensor_list.append(coeff_info.local_temp)
            coeff_offset_list.append(coeff_info.offset_index)

    coeff_no = 0
    for ordered_coeff in builder.expression.coefficients():
        try:
            indices = [i for i, x in enumerate(coeff_function_list) if x == ordered_coeff]
            for func_index in indices:
                loopy_tensor = builder.gem_loopy_dict[coeff_tensor_list[func_index]]
                loopy_outer.temporary_variables[loopy_tensor.name] = loopy_tensor
                indices = builder.create_index(coeff_shape_list[func_index], str(coeff_tensor_list[func_index])+"_init"+str(coeff_no))
                builder.gem_indices[str(coeff_tensor_list[func_index])+"_init"+str(coeff_no)]
                inames = {var.name for var in indices}
                inits.append(lp.Assignment(pym.Subscript(pym.Variable(loopy_tensor.name), (pym.Sum((coeff_offset_list[func_index], indices[0])),)), pym.Subscript(pym.Variable("coeff%d" % coeff_no), indices), id="init%d" % c, within_inames=frozenset(inames)))
                c += 1
                coeff_no += 1
        except ValueError:
            pass

    # Generate temp e.g. for plexmesh_exterior_local_facet_number (maps from global to local facets)
    if builder.needs_cell_facets:
        loopy_outer.temporary_variables["facet_array"] = lp.TemporaryVariable(builder.local_facet_array_arg,
                                                                              shape=(builder.num_facets, 2),
                                                                              dtype=np.uint32,
                                                                              address_space=lp.AddressSpace.LOCAL,
                                                                              read_only=True,
                                                                              initializer=np.arange(builder.num_facets, dtype=np.uint32))

    # Get the CallInstruction for each kernel from builder
    kitting_insn = []
    for integral_type in builder.assembly_calls:
        kitting_insn += builder.assembly_calls[integral_type]

    loopy_merged = loopy_outer.copy(instructions=inits+kitting_insn+loopy_outer.instructions)

    # Generate dependencies
    loopy_merged = add_sequential_dependencies(loopy_merged)

    # Remove priority generated from tsfc compile call
    for insn in loopy_merged.instructions[-len(loopy_outer.instructions):]:
        loopy_merged = lp.set_instruction_priority(loopy_merged, "id:"+insn.id, None)

    # Fix domains (add additional indices coming from calling the subkernel)
    def create_domains(gem_indices):
        for tuple_index in gem_indices:
            for i in tuple_index:
                name = i.name
                extent = i.extent
                isl.make_zero_and_vars([name], [])
                yield BasicSet("{ ["+name+"]: 0<="+name+"<"+str(extent)+"}")
    domains = list(create_domains(builder.gem_indices.values()))
    loopy_merged = loopy_merged.copy(domains=domains+loopy_merged.domains)

    # Generate program from kernel, register inner kernel and inline inner kernel
    prg = make_program(loopy_merged)
    for loopy_inner in loopy_inner_list:
        prg = register_callable_kernel(prg, loopy_inner)
        prg = inline_callable_kernel(prg, loopy_inner.name)
    return prg


# My own dependecy generation
# Out of date, I use TSFCs dependency generation now
def my_dependency_generation(loopy_outer, loopy_merged, kitting_insn, builder, inits):
    noi_outer = len(loopy_outer.instructions)
    noi_inits = len(inits)
    # # add dependencies dynamically
    if len(loopy_outer.instructions) > 1:
        for i in range(len(kitting_insn)):
            # add dep from first insn of outer kernel to all subkernels
            loopy_merged = lp.add_dependency(loopy_merged, "id:"+loopy_merged.instructions[-noi_outer].id, "id:"+loopy_merged.instructions[-noi_outer-i-1].id)

            # dep from subkernel to the according init
            # loopy_merged= lp.add_dependency(loopy_merged, "id:"+loopy_merged.instructions[noi_inits+i].id,  "id:"+loopy_merged.instructions[noi_inits-i-1].id)
            loopy_merged = lp.add_dependency(loopy_merged, "id:"+loopy_merged.instructions[noi_inits+i].id, "id:"+loopy_merged.instructions[i].id)

    elif not len(kitting_insn) == 0:
        for i, assembly_call in enumerate(kitting_insn):
            # add dep from first insn of outer kernel to the subkernel in first loop
            # then from subkernel to subkernel
            loopy_merged = lp.add_dependency(loopy_merged, "id:"+loopy_merged.instructions[-noi_outer-i].id, "id:"+loopy_merged.instructions[-noi_outer-i-1].id)

    # # dep from first subkernel to the according init# TODO do we need this?
    loopy_merged = lp.add_dependency(loopy_merged, "id:"+loopy_merged.instructions[-noi_outer-len(kitting_insn)].id, "id:"+loopy_merged.instructions[0].id)

    # # link to initilisaton of vectemps, TODO: this is not robust
    for k, v in builder.coefficient_vecs.items():
        loopy_merged = lp.add_dependency(loopy_merged, "id:"+loopy_merged.instructions[-noi_outer+len(builder.temps)].id, "id:"+loopy_merged.instructions[len(builder.temps)].id)
