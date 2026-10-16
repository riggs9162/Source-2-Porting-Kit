using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

/// <summary>Serializes exact convex polyhedra through a selected offline Source VPhysics library.</summary>
public static class ConvexSerializer
{
    [StructLayout(LayoutKind.Sequential)]
    private struct Vector3
    {
        public float x;
        public float y;
        public float z;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadLibrary(string filename);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetDllDirectory(string directory);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate IntPtr Factory([MarshalAs(UnmanagedType.LPStr)] string name, out int code);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    private delegate IntPtr FromPolyhedron(IntPtr self, IntPtr polyhedron);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    private delegate IntPtr ConvertConvexes(IntPtr self, IntPtr convexes, int count);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    private delegate int CollideSize(IntPtr self, IntPtr collide);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    private delegate int CollideWrite(IntPtr self, IntPtr destination, IntPtr collide, [MarshalAs(UnmanagedType.I1)] bool swap);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    private delegate void FreeNative(IntPtr self, IntPtr value);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    private delegate void GetAabb(IntPtr self, out Vector3 minimum, out Vector3 maximum, IntPtr collide, ref Vector3 origin, ref Vector3 angles);

    /// <summary>Resolves a method in the fixed VPhysicsCollision007 interface contract.</summary>
    private static T Method<T>(IntPtr instance, int index) where T : class
    {
        IntPtr table = Marshal.ReadIntPtr(instance);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(Marshal.ReadIntPtr(table, index * 4), typeof(T));
    }

    /// <summary>Copies a bounded complete input array into unmanaged storage owned by the current operation.</summary>
    private static IntPtr CopyBytes(BinaryReader reader, int count, List<IntPtr> buffers)
    {
        byte[] bytes = reader.ReadBytes(count);
        if (bytes.Length != count)
            throw new EndOfStreamException("Truncated convex geometry input");
        IntPtr memory = Marshal.AllocHGlobal(count);
        buffers.Add(memory);
        Marshal.Copy(bytes, 0, memory, count);
        return memory;
    }

    /// <summary>Builds a convex from the Source SDK CPolyhedron layout without point-cloud or plane simplification.</summary>
    private static IntPtr ReadConvex(BinaryReader reader, IntPtr collision)
    {
        int vertices = reader.ReadUInt16();
        int lines = reader.ReadUInt16();
        int indices = reader.ReadUInt16();
        int polygons = reader.ReadUInt16();
        if (vertices < 4 || lines < 6 || polygons < 4 || indices != polygons * 3)
            throw new InvalidDataException("Invalid convex polyhedron counts");
        List<IntPtr> buffers = new List<IntPtr>();
        try
        {
            IntPtr polyhedron = Marshal.AllocHGlobal(28);
            buffers.Add(polyhedron);
            Marshal.WriteIntPtr(polyhedron, 0, IntPtr.Zero);
            Marshal.WriteIntPtr(polyhedron, 4, CopyBytes(reader, vertices * 12, buffers));
            Marshal.WriteIntPtr(polyhedron, 8, CopyBytes(reader, lines * 4, buffers));
            Marshal.WriteIntPtr(polyhedron, 12, CopyBytes(reader, indices * 4, buffers));
            Marshal.WriteIntPtr(polyhedron, 16, CopyBytes(reader, polygons * 16, buffers));
            Marshal.WriteInt16(polyhedron, 20, unchecked((short)vertices));
            Marshal.WriteInt16(polyhedron, 22, unchecked((short)lines));
            Marshal.WriteInt16(polyhedron, 24, unchecked((short)indices));
            Marshal.WriteInt16(polyhedron, 26, unchecked((short)polygons));
            IntPtr convex = Method<FromPolyhedron>(collision, 8)(collision, polyhedron);
            if (convex == IntPtr.Zero)
                throw new InvalidOperationException("Native convex polyhedron construction failed");
            return convex;
        }
        finally
        {
            foreach (IntPtr buffer in buffers)
                Marshal.FreeHGlobal(buffer);
        }
    }

    /// <summary>Produces one complete native compact surface while retaining separate authored convex components.</summary>
    private static byte[] ReadBody(BinaryReader reader, IntPtr collision, out float[] bounds)
    {
        int count = reader.ReadInt32();
        if (count < 1 || count > 4096)
            throw new InvalidDataException("Invalid convex count for a physics body");
        List<IntPtr> convexes = new List<IntPtr>();
        IntPtr pointers = IntPtr.Zero;
        IntPtr collide = IntPtr.Zero;
        IntPtr destination = IntPtr.Zero;
        try
        {
            for (int index = 0; index < count; index++)
                convexes.Add(ReadConvex(reader, collision));
            pointers = Marshal.AllocHGlobal(count * 4);
            for (int index = 0; index < count; index++)
                Marshal.WriteIntPtr(pointers, index * 4, convexes[index]);
            collide = Method<ConvertConvexes>(collision, 14)(collision, pointers, count);
            convexes.Clear();
            if (collide == IntPtr.Zero)
                throw new InvalidOperationException("Native compact-surface construction failed");
            int size = Method<CollideSize>(collision, 17)(collision, collide);
            if (size < 76 || size > 64 * 1024 * 1024)
                throw new InvalidOperationException("Native serializer returned an invalid surface size");
            destination = Marshal.AllocHGlobal(size);
            int written = Method<CollideWrite>(collision, 18)(collision, destination, collide, false);
            if (written != size)
                throw new InvalidOperationException("Native serializer returned an incomplete compact surface");
            byte[] output = new byte[written];
            Marshal.Copy(destination, output, 0, written);
            Vector3 minimum, maximum;
            Vector3 origin = new Vector3();
            Vector3 angles = new Vector3();
            Method<GetAabb>(collision, 23)(collision, out minimum, out maximum, collide, ref origin, ref angles);
            bounds = new float[] { minimum.x, minimum.y, minimum.z, maximum.x, maximum.y, maximum.z };
            return output;
        }
        finally
        {
            foreach (IntPtr convex in convexes)
                Method<FreeNative>(collision, 6)(collision, convex);
            if (collide != IntPtr.Zero)
                Method<FreeNative>(collision, 16)(collision, collide);
            if (pointers != IntPtr.Zero)
                Marshal.FreeHGlobal(pointers);
            if (destination != IntPtr.Zero)
                Marshal.FreeHGlobal(destination);
        }
    }

    /// <summary>Runs in a separate x86 process so offline compiler libraries never enter the porting application's process.</summary>
    public static int Main(string[] arguments)
    {
        try
        {
            if (arguments.Length != 3)
                throw new ArgumentException("Usage: convex_serializer <x86 VPhysics DLL> <input> <output>");
            string library = Path.GetFullPath(arguments[0]);
            SetDllDirectory(Path.GetDirectoryName(library));
            IntPtr module = LoadLibrary(library);
            if (module == IntPtr.Zero)
                throw new InvalidOperationException("Cannot load the selected x86 VPhysics DLL and its dependencies: Windows error " + Marshal.GetLastWin32Error());
            IntPtr entry = GetProcAddress(module, "CreateInterface");
            if (entry == IntPtr.Zero)
                throw new InvalidOperationException("The selected VPhysics DLL has no CreateInterface export");
            Factory factory = (Factory)Marshal.GetDelegateForFunctionPointer(entry, typeof(Factory));
            int status;
            IntPtr collision = factory("VPhysicsCollision007", out status);
            if (collision == IntPtr.Zero)
                throw new InvalidOperationException("The selected compiler library does not provide VPhysicsCollision007");
            using (BinaryReader reader = new BinaryReader(File.OpenRead(arguments[1])))
            using (BinaryWriter writer = new BinaryWriter(File.Create(arguments[2])))
            {
                if (reader.ReadUInt32() != 0x56433253 || reader.ReadInt32() != 1)
                    throw new InvalidDataException("Unsupported convex serialization input");
                int bodies = reader.ReadInt32();
                if (bodies < 1 || bodies > 32)
                    throw new InvalidDataException("Physics body count must be between 1 and 32");
                writer.Write(0x56503253U);
                writer.Write(2);
                writer.Write(bodies);
                for (int index = 0; index < bodies; index++)
                {
                    float[] bounds;
                    byte[] blob = ReadBody(reader, collision, out bounds);
                    writer.Write(blob.Length);
                    writer.Write(blob);
                    foreach (float value in bounds)
                        writer.Write(value);
                    Console.WriteLine("Serialized body {0}/{1}: {2} bytes", index + 1, bodies, blob.Length);
                }
                if (reader.BaseStream.Position != reader.BaseStream.Length)
                    throw new InvalidDataException("Unexpected trailing convex input data");
            }
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception.GetType().Name + ": " + exception.Message);
            return 1;
        }
    }
}
